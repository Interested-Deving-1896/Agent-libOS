from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time
from uuid import uuid4

import pytest
from pydantic import BaseModel

from agent_libos import Runtime
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models import (
    CapabilityRight,
    ObjectHandle,
    ObjectMetadata,
    ObjectPatch,
    ObjectRight,
    ObjectTaskNotificationStatus,
    ObjectTaskStatus,
    ObjectType,
    ProcessMessageKind,
    ProcessStatus,
    RelationType,
)
from agent_libos.models.exceptions import CapabilityDenied, ProcessMessageWaitRequired, ValidationError
from agent_libos.substrate import LocalResourceProviderSubstrate
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolPolicy


class EmptyArgs(BaseModel):
    pass


class SideEffectThenWaitTool(SyncAgentTool[EmptyArgs]):
    name = "side_effect_then_wait"
    description = "Record a side effect before blocking on an owner-watch message."
    args_schema = EmptyArgs
    policy = ToolPolicy(side_effects=True, idempotent=False)

    def __init__(self, counter: dict[str, int]) -> None:
        self.counter = counter

    def run(self, args: EmptyArgs, ctx: ToolContext) -> dict[str, object]:
        runtime = ctx.runtime
        assert runtime is not None
        self.counter["calls"] = self.counter.get("calls", 0) + 1
        runtime.messages.receive(ctx.pid, block=True, channel=runtime.config.object_tasks.owner_watch_channel)
        return {"ready": True}


class SlowSyncSideEffectTool(SyncAgentTool[EmptyArgs]):
    name = "slow_sync_side_effect_for_object_task"
    description = "Slow sync side-effect tool used to exercise cancellation semantics."
    args_schema = EmptyArgs
    policy = ToolPolicy(side_effects=True, idempotent=False)

    def run(self, args: EmptyArgs, ctx: ToolContext) -> dict[str, object]:
        time.sleep(0.2)
        return {"ok": True}


class TestObjectTasks:
    def test_object_task_runs_visible_tool_links_result_and_notifies_creator(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="object task")
            owner = runtime.memory.create_object(
                pid,
                ObjectType.ARTIFACT,
                {"name": "owner"},
                metadata=ObjectMetadata(title="owner"),
                immutable=False,
            )

            task = runtime.object_tasks.start(pid, owner, "get_working_directory", {})
            completed = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert completed.result_oid is not None
            assert completed.notification.status == ObjectTaskNotificationStatus.DELIVERED
            assert runtime.store.get_object(completed.result_oid) is not None
            links = runtime.store.list_links(src=owner.oid)
            assert [(link.relation, link.dst) for link in links] == [(RelationType.PRODUCED, completed.result_oid)]
            unread = runtime.messages.unread(pid)
            assert unread[-1].sender == f"object_task:{task.task_id}"
            assert unread[-1].channel == runtime.config.object_tasks.notification_channel
            assert unread[-1].payload["status"] == ObjectTaskStatus.SUCCEEDED.value
        finally:
            runtime.close()

    def test_object_task_cannot_run_tool_outside_creator_tool_table(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="object task")
            owner = _owner(runtime, pid)

            with pytest.raises(ValidationError, match="not in process tool table"):
                runtime.object_tasks.start(pid, owner, "parse_pytest_log", {"log": "FAILED tests/x.py::test_y"})
        finally:
            runtime.close()

    def test_object_task_start_requires_owner_write_and_link_rights(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="object task")
            read_only = runtime.memory.create_object(
                pid,
                ObjectType.ARTIFACT,
                {"name": "immutable"},
                metadata=ObjectMetadata(title="immutable"),
                immutable=True,
            )

            with pytest.raises(CapabilityDenied, match="write"):
                runtime.object_tasks.start(pid, read_only, "get_working_directory", {})
        finally:
            runtime.close()

    def test_object_task_runner_does_not_inherit_external_capability_unless_explicit(self, tmp_path: Path) -> None:
        runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(tmp_path))
        try:
            path = f"agent_outputs/object_task_{uuid4().hex}.txt"
            pid = runtime.process.spawn(image="review-agent:v0", goal="write from object task")
            owner = _owner(runtime, pid)
            runtime.filesystem.grant_path(pid, path, [CapabilityRight.WRITE], issued_by="test", delegable=True)

            denied = runtime.object_tasks.start(pid, owner, "write_text_file", {"path": path, "content": "denied"})
            denied = runtime.object_tasks.wait(denied.task_id, actor_pid=pid, timeout=2)
            assert denied.status == ObjectTaskStatus.FAILED
            assert not (tmp_path / path).exists()

            allowed = runtime.object_tasks.start(
                pid,
                owner,
                "write_text_file",
                {"path": path, "content": "allowed"},
                inherit_capabilities=[
                    {"resource": runtime.filesystem.resource_for(path), "rights": [CapabilityRight.WRITE.value]}
                ],
            )
            allowed = runtime.object_tasks.wait(allowed.task_id, actor_pid=pid, timeout=2)
            assert allowed.status == ObjectTaskStatus.SUCCEEDED
            assert (tmp_path / path).read_text(encoding="utf-8") == "allowed"
        finally:
            runtime.close()

    def test_object_task_runner_is_not_scheduled_by_llm_scheduler(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="scheduler isolation")
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(pid, owner, "receive_process_messages", {"channel": "never"})
            waiting = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)
            assert waiting.status == ObjectTaskStatus.WAITING_MESSAGE

            runner = runtime.process.get(str(waiting.runner_pid))
            runner.status = ProcessStatus.RUNNABLE
            runtime.store.update_process(runner)

            assert str(waiting.runner_pid) not in runtime.scheduler.runnable_pids()
            results = runtime.scheduler.run_until_idle(lambda selected_pid: {"pid": selected_pid}, max_quanta=2)
            assert all(item.get("pid") != waiting.runner_pid for item in results if isinstance(item, dict))
            assert not any(
                record.action == "scheduler.run_quantum" and record.target == f"process:{waiting.runner_pid}"
                for record in runtime.audit.trace()
            )
        finally:
            runtime.close()

    def test_object_task_completion_survives_one_time_owner_handle(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="one time owner")
            child = runtime.spawn_child_process(parent, "object task creator")
            owner = _owner(runtime, parent)
            cap = runtime.capability.issue_trusted(
                subject=child,
                resource=f"object:{owner.oid}",
                rights=[ObjectRight.READ.value, ObjectRight.WRITE.value, ObjectRight.LINK.value],
                issued_by="test",
                uses_remaining=1,
            )
            one_time_owner = ObjectHandle(
                oid=owner.oid,
                rights={ObjectRight.READ.value, ObjectRight.WRITE.value, ObjectRight.LINK.value},
                capability_id=cap.cap_id,
            )

            task = runtime.object_tasks.start(child, one_time_owner, "get_working_directory", {})
            completed = runtime.object_tasks.wait(task.task_id, actor_pid=child, timeout=2)

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert runtime.store.get_capability(cap.cap_id).uses_remaining == 0
            assert [(link.relation, link.dst) for link in runtime.store.list_links(src=owner.oid)] == [
                (RelationType.PRODUCED, completed.result_oid)
            ]
        finally:
            runtime.close()

    def test_object_task_notification_can_interrupt_and_wake_message_waiter(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            child = runtime.spawn_child_process(parent, "wait for task")
            owner = _owner(runtime, parent)
            with pytest.raises(ProcessMessageWaitRequired):
                runtime.messages.receive(child, block=True, channel="object-task")
            assert runtime.process.get(child).status == ProcessStatus.WAITING_EVENT

            task = runtime.object_tasks.start(
                parent,
                owner,
                "get_working_directory",
                {},
                notify_pid=child,
                notify_kind=ProcessMessageKind.INTERRUPT,
            )
            completed = runtime.object_tasks.wait(task.task_id, actor_pid=parent, timeout=2)

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert runtime.process.get(child).status == ProcessStatus.RUNNABLE
            message = runtime.messages.unread(child)[0]
            assert message.kind == ProcessMessageKind.INTERRUPT
            assert message.channel == "object-task"
            assert message.payload["task_id"] == task.task_id
        finally:
            runtime.close()

    def test_object_task_records_undelivered_notification_when_target_exits(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            child = runtime.spawn_child_process(parent, "notify me")
            owner = _owner(runtime, parent)
            task = runtime.object_tasks.start(parent, owner, "sleep", {"seconds": 0.05}, notify_pid=child)
            runtime.process.exit(child, message="done")

            completed = runtime.object_tasks.wait(task.task_id, actor_pid=parent, timeout=2)

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert completed.notification.status == ObjectTaskNotificationStatus.UNDELIVERED_TERMINAL
            assert runtime.messages.unread(child) == []
        finally:
            runtime.close()

    def test_object_task_result_oid_in_notification_does_not_grant_result_authority(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            child = runtime.spawn_child_process(parent, "notify me")
            owner = _owner(runtime, parent)
            task = runtime.object_tasks.start(parent, owner, "get_working_directory", {}, notify_pid=child)
            completed = runtime.object_tasks.wait(task.task_id, actor_pid=parent, timeout=2)
            result_oid = completed.result_oid
            assert result_oid is not None

            with pytest.raises(CapabilityDenied):
                runtime.memory.handle_for_oid(child, result_oid, required_rights={ObjectRight.READ.value})
            parent_handle = runtime.memory.handle_for_oid(parent, result_oid, required_rights={ObjectRight.READ.value})
            assert parent_handle.oid == result_oid
        finally:
            runtime.close()

    def test_object_task_waiting_message_state_does_not_repeat_tool_call(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="wait task")
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(
                pid,
                owner,
                "receive_process_messages",
                {"channel": "never"},
            )
            waiting = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)

            assert waiting.status == ObjectTaskStatus.WAITING_MESSAGE
            assert waiting.wait["filters"]["channel"] == "never"
            assert runtime.process.get(str(waiting.runner_pid)).status == ProcessStatus.WAITING_EVENT
            assert len([record for record in runtime.audit.trace() if record.action == "tool.call_waiting_message"]) == 1
        finally:
            runtime.close()

    def test_object_task_request_permission_resumes_after_human_decision(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="permission task")
            owner = _owner(runtime, pid)
            runtime.capability.grant(pid, "human:owner", [CapabilityRight.WRITE], issued_by="test", delegable=True)
            resource = "filesystem:workspace:agent_outputs/object_task_permission.txt"

            task = runtime.object_tasks.start(
                pid,
                owner,
                "request_permission",
                {"resource": resource, "rights": [CapabilityRight.WRITE.value], "reason": "write artifact"},
                inherit_capabilities=[
                    {"resource": "human:owner", "rights": [CapabilityRight.WRITE.value]},
                ],
            )
            waiting = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)
            assert waiting.status == ObjectTaskStatus.WAITING_HUMAN
            assert waiting.wait["request_id"] == runtime.human.pending()[0].request_id

            runtime.human.drain_terminal_queue(auto_policy=runtime.capability.ALWAYS_ALLOW)
            completed = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            payload = runtime.store.get_object(str(completed.result_oid)).payload
            assert payload["result"]["status"] == "approved"
            assert any(record.action == "object_task.human_resume" for record in runtime.audit.trace())
        finally:
            runtime.close()

    def test_object_task_owner_watch_update_resumes_waiting_runner(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="watch owner")
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(
                pid,
                owner,
                "receive_process_messages",
                {"channel": runtime.config.object_tasks.owner_watch_channel},
                owner_watch=True,
            )
            waiting = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)
            assert waiting.status == ObjectTaskStatus.WAITING_MESSAGE

            runtime.memory.update_object(pid, owner, ObjectPatch(payload={"name": "owner", "version": 2}))
            completed = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert completed.owner_watch.enabled
            assert completed.result_oid is not None
            payload = runtime.store.get_object(completed.result_oid).payload
            message = payload["result"]["messages"][0]
            assert message["channel"] == runtime.config.object_tasks.owner_watch_channel
            assert message["payload"]["type"] == "object_task_owner_change"
            assert message["payload"]["event"] == "updated"
            assert message["payload"]["owner_oid"] == owner.oid
            assert message["payload"]["version"] == 2
            assert "payload" not in message["payload"]
            assert any(record.action == "object_task.owner_watch.resume" for record in runtime.audit.trace())
        finally:
            runtime.close()

    def test_object_task_owner_watch_does_not_replay_unsafe_waiting_tool(self) -> None:
        runtime = Runtime.open("local")
        try:
            counter: dict[str, int] = {}
            handle = runtime.tools.register_tool(SideEffectThenWaitTool(counter), registered_by="test", ephemeral=True)
            pid = runtime.process.spawn(image="base-agent:v0", goal="unsafe replay")
            runtime.tools.configure_process_tools(pid, [handle], assigned_by="test")
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(pid, owner, "side_effect_then_wait", {}, owner_watch=True)
            waiting = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)
            assert waiting.status == ObjectTaskStatus.WAITING_MESSAGE
            assert counter["calls"] == 1

            runtime.memory.update_object(pid, owner, ObjectPatch(payload={"name": "owner", "version": 2}))
            still_waiting = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=0.1)

            assert still_waiting.status == ObjectTaskStatus.WAITING_MESSAGE
            assert counter["calls"] == 1
            assert runtime.messages.unread(str(task.runner_pid), channel=runtime.config.object_tasks.owner_watch_channel)
            assert any(
                record.action == "object_task.owner_watch.resume_unsafe_replay_skipped"
                for record in runtime.audit.trace()
            )
        finally:
            runtime.close()

    def test_object_task_owner_watch_link_payload_does_not_grant_dst_authority(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="watch link")
            child = runtime.spawn_child_process(parent, "watcher")
            owner = _owner(runtime, parent)
            dst = runtime.memory.create_object(
                parent,
                ObjectType.ARTIFACT,
                {"secret": "dst"},
                metadata=ObjectMetadata(title="dst"),
                immutable=True,
            )
            runtime.capability.grant(
                subject=child,
                resource=f"object:{owner.oid}",
                rights=[ObjectRight.READ.value, ObjectRight.WRITE.value, ObjectRight.LINK.value],
                issued_by="test",
            )
            child_owner = runtime.memory.handle_for_oid(
                child,
                owner.oid,
                required_rights={ObjectRight.READ.value, ObjectRight.WRITE.value, ObjectRight.LINK.value},
            )
            task = runtime.object_tasks.start(
                child,
                child_owner,
                "receive_process_messages",
                {"channel": "owner-link-watch"},
                owner_watch={"enabled": True, "events": ["linked"], "channel": "owner-link-watch"},
            )
            waiting = runtime.object_tasks.wait(task.task_id, actor_pid=child, timeout=2)
            assert waiting.status == ObjectTaskStatus.WAITING_MESSAGE

            runtime.memory.link_objects(parent, owner, RelationType.REFERENCES, dst)
            completed = runtime.object_tasks.wait(task.task_id, actor_pid=child, timeout=2)

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            payload = runtime.store.get_object(completed.result_oid).payload
            message = payload["result"]["messages"][0]
            assert message["payload"]["event"] == "linked"
            assert message["payload"]["relation"] == RelationType.REFERENCES.value
            assert message["payload"]["dst_oid"] == dst.oid
            with pytest.raises(CapabilityDenied):
                runtime.memory.handle_for_oid(child, dst.oid, required_rights={ObjectRight.READ.value})
        finally:
            runtime.close()

    def test_object_task_owner_watch_disabled_does_not_notify_runner(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="watch disabled")
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(
                pid,
                owner,
                "receive_process_messages",
                {"channel": runtime.config.object_tasks.owner_watch_channel},
            )
            waiting = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)
            assert waiting.status == ObjectTaskStatus.WAITING_MESSAGE

            runtime.memory.update_object(pid, owner, ObjectPatch(payload={"name": "owner", "version": 2}))

            still_waiting = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=0.05)
            assert still_waiting.status == ObjectTaskStatus.WAITING_MESSAGE
            assert runtime.messages.unread(str(task.runner_pid), channel=runtime.config.object_tasks.owner_watch_channel) == []
        finally:
            runtime.close()

    def test_object_task_owner_watch_terminal_task_is_not_notified(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="watch terminal")
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(
                pid,
                owner,
                "get_working_directory",
                {},
                owner_watch=True,
            )
            completed = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)
            assert completed.status == ObjectTaskStatus.SUCCEEDED
            before = len(runtime.store.list_process_messages(str(completed.runner_pid)))

            runtime.memory.update_object(pid, owner, ObjectPatch(payload={"name": "owner", "version": 2}))

            after = len(runtime.store.list_process_messages(str(completed.runner_pid)))
            assert after == before
        finally:
            runtime.close()

    def test_object_task_pins_owner_object_after_creator_exit_until_task_completes(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="owner exits")
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(pid, owner, "sleep", {"seconds": 0.05})
            runtime.process.exit(pid, message="creator exited")

            assert runtime.store.get_object(owner.oid) is not None
            completed = runtime.object_tasks.wait(task.task_id, timeout=2)

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert runtime.store.get_object(owner.oid) is None
            assert completed.notification.status == ObjectTaskNotificationStatus.UNDELIVERED_TERMINAL
        finally:
            runtime.close()

    def test_object_task_cancel_updates_task_and_runner_process(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="cancel task")
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(pid, owner, "sleep", {"seconds": 1.0})

            cancelled = runtime.object_tasks.cancel(task.task_id, actor_pid=pid, reason="no longer needed")

            assert cancelled.status == ObjectTaskStatus.CANCELLED
            assert runtime.process.get(str(cancelled.runner_pid)).status == ProcessStatus.KILLED
        finally:
            runtime.close()

    def test_object_task_refuses_to_cancel_running_sync_side_effect_tool(self) -> None:
        runtime = Runtime.open("local")
        try:
            handle = runtime.tools.register_tool(SlowSyncSideEffectTool(), registered_by="test", ephemeral=True)
            pid = runtime.process.spawn(image="base-agent:v0", goal="sync cancel")
            runtime.tools.configure_process_tools(pid, [handle], assigned_by="test")
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(pid, owner, "slow_sync_side_effect_for_object_task", {})
            deadline = time.monotonic() + 1.0
            while runtime.object_tasks.get(task.task_id, actor_pid=pid).status != ObjectTaskStatus.RUNNING:
                if time.monotonic() >= deadline:
                    pytest.fail("object task did not enter running state")
                time.sleep(0.01)

            with pytest.raises(ValidationError, match="cannot be safely cancelled"):
                runtime.object_tasks.cancel(task.task_id, actor_pid=pid)

            completed = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)
            assert completed.status == ObjectTaskStatus.SUCCEEDED
        finally:
            runtime.close()

    def test_object_task_reconciles_external_runner_kill(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="external kill")
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(pid, owner, "receive_process_messages", {"channel": "never"})
            waiting = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)
            assert waiting.status == ObjectTaskStatus.WAITING_MESSAGE

            runtime.process.signal(str(waiting.runner_pid), "cancel", {"reason": "external kill"})
            refreshed = runtime.object_tasks.get(task.task_id, actor_pid=pid)

            assert refreshed.status == ObjectTaskStatus.CANCELLED
            assert refreshed.error == "external kill"
        finally:
            runtime.close()

    def test_object_task_list_limit_applies_after_visibility_filter(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            child = runtime.spawn_child_process(parent, "child")
            child_owner = _owner(runtime, child)
            child_task = runtime.object_tasks.start(child, child_owner, "get_working_directory", {})
            child_task = runtime.object_tasks.wait(child_task.task_id, actor_pid=child, timeout=2)
            parent_owner = _owner(runtime, parent)
            parent_task = runtime.object_tasks.start(parent, parent_owner, "get_working_directory", {})
            runtime.object_tasks.wait(parent_task.task_id, actor_pid=parent, timeout=2)

            visible = runtime.object_tasks.list(actor_pid=child, limit=1)

            assert [task.task_id for task in visible] == [child_task.task_id]
        finally:
            runtime.close()

    def test_object_task_per_object_concurrency_limit_is_enforced(self) -> None:
        config = replace(
            DEFAULT_CONFIG,
            object_tasks=replace(DEFAULT_CONFIG.object_tasks, max_running_per_object=1),
        )
        runtime = Runtime.open("local", config=config)
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="concurrency")
            owner = _owner(runtime, pid)
            first = runtime.object_tasks.start(
                pid,
                owner,
                "receive_process_messages",
                {"channel": "never"},
            )
            waiting = runtime.object_tasks.wait(first.task_id, actor_pid=pid, timeout=2)
            assert waiting.status == ObjectTaskStatus.WAITING_MESSAGE

            with pytest.raises(ValidationError, match="per-object concurrency limit"):
                runtime.object_tasks.start(pid, owner, "get_working_directory", {})
        finally:
            runtime.close()


def _owner(runtime: Runtime, pid: str):
    return runtime.memory.create_object(
        pid,
        ObjectType.ARTIFACT,
        {"name": "owner"},
        metadata=ObjectMetadata(title="owner"),
        immutable=False,
    )

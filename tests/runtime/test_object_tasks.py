from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import threading
import time
from uuid import uuid4

import pytest
from pydantic import BaseModel

from agent_libos import Runtime
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models import (
    CapabilityRight,
    EventType,
    ObjectHandle,
    ObjectMetadata,
    ObjectOwnerKind,
    ObjectPatch,
    ObjectRight,
    ObjectTask,
    ObjectTaskNotificationStatus,
    ObjectTaskStatus,
    ObjectType,
    ProcessMessageKind,
    ProcessStatus,
    RelationType,
)
from agent_libos.models.exceptions import CapabilityDenied, ProcessError, ProcessMessageWaitRequired, ValidationError
from agent_libos.substrate import LocalResourceProviderSubstrate
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolPolicy


def _grant_process_spawn(runtime: Runtime, pid: str) -> None:
    runtime.capability.grant(pid, "process:spawn", [CapabilityRight.WRITE], issued_by="test")


def _grant_delegable_clock_sleep(runtime: Runtime, pid: str) -> None:
    runtime.capability.grant(pid, "clock:sleep", [CapabilityRight.READ], issued_by="test", delegable=True)


def _inherit_clock_sleep() -> list[dict[str, object]]:
    return [{"resource": "clock:sleep", "rights": [CapabilityRight.READ.value]}]


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
    def test_runtime_reopen_marks_terminal_result_unavailable_when_payload_was_runtime_only(
        self,
        tmp_path: Path,
    ) -> None:
        database = tmp_path / "object-task-result-reopen.sqlite"
        runtime = Runtime.open(database)
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="persist ObjectTask history")
            _grant_process_spawn(runtime, pid)
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(pid, owner, "get_working_directory", {})
            completed = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=3)
            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert completed.completed_at is not None
            assert completed.result_oid is not None
            result_oid = str(completed.result_oid)
            completed_at = completed.completed_at
        finally:
            runtime.close()

        reopened = Runtime.open(database)
        try:
            recovered = reopened.object_tasks.get(task.task_id, actor_pid=pid)
            assert recovered.status == ObjectTaskStatus.RESULT_UNAVAILABLE_AFTER_REOPEN
            assert recovered.result_oid is None
            assert recovered.completed_at == completed_at
            assert recovered.wait["result_unavailable_after_reopen"] is True
            assert recovered.wait["previous_status"] == ObjectTaskStatus.SUCCEEDED.value
            assert recovered.wait["previous_result_oid"] == result_oid
            assert "runtime reopen" in str(recovered.error)
            assert reopened.store.get_object(result_oid) is None
            assert any(
                entry.action == "object_task.result_unavailable_recovered"
                and task.task_id in entry.decision.get("task_ids", [])
                for entry in reopened.audit.trace()
            )
        finally:
            reopened.close()

        reopened_again = Runtime.open(database)
        try:
            recovered_again = reopened_again.object_tasks.get(task.task_id, actor_pid=pid)
            assert recovered_again.status == ObjectTaskStatus.RESULT_UNAVAILABLE_AFTER_REOPEN
            assert recovered_again.result_oid is None
            assert recovered_again.completed_at == completed_at
            assert recovered_again.wait["previous_result_oid"] == result_oid
            assert len(
                [
                    entry
                    for entry in reopened_again.audit.trace()
                    if entry.action == "object_task.result_unavailable_recovered"
                    and task.task_id in entry.decision.get("task_ids", [])
                ]
            ) == 1
        finally:
            reopened_again.close()

    def test_cross_actor_task_view_and_cancel_consume_one_shot_authority(self) -> None:
        runtime = Runtime.open("local")
        try:
            creator = runtime.process.spawn(image="base-agent:v0", goal="task creator")
            actor = runtime.process.spawn(image="base-agent:v0", goal="task controller")
            _grant_process_spawn(runtime, creator)
            owner = _owner(runtime, creator)
            tasks = [
                runtime.object_tasks.start(
                    creator,
                    owner,
                    "receive_process_messages",
                    {"channel": f"never-{index}"},
                )
                for index in range(2)
            ]
            for task in tasks:
                waiting = runtime.object_tasks.wait(task.task_id, actor_pid=creator, timeout=2)
                assert waiting.status == ObjectTaskStatus.WAITING_MESSAGE

            write_cap = runtime.capability.issue_trusted(
                actor,
                f"object:{owner.oid}",
                [ObjectRight.WRITE],
                issued_by="test",
                uses_remaining=1,
            )
            cancelled = runtime.object_tasks.cancel(tasks[0].task_id, actor_pid=actor)
            assert cancelled.status == ObjectTaskStatus.CANCELLED
            assert runtime.store.get_capability(write_cap.cap_id).uses_remaining == 0
            with pytest.raises(CapabilityDenied):
                runtime.object_tasks.cancel(tasks[1].task_id, actor_pid=actor)

            read_cap = runtime.capability.issue_trusted(
                actor,
                f"object:{owner.oid}",
                [ObjectRight.READ],
                issued_by="test",
                uses_remaining=1,
            )
            assert runtime.object_tasks.get(tasks[1].task_id, actor_pid=actor).task_id == tasks[1].task_id
            assert runtime.store.get_capability(read_cap.cap_id).uses_remaining == 0
            with pytest.raises(CapabilityDenied):
                runtime.object_tasks.get(tasks[1].task_id, actor_pid=actor)
        finally:
            runtime.close()
    def test_object_task_runs_visible_tool_links_result_and_notifies_creator(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="object task")
            _grant_process_spawn(runtime, pid)
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
            assert runtime.store.get_object(completed.result_oid).owner_kind == ObjectOwnerKind.OBJECT_TASK
            assert runtime.store.get_object(completed.result_oid).owner_id == task.task_id
            links = runtime.store.list_links(src=owner.oid)
            assert [(link.relation, link.dst) for link in links] == [(RelationType.PRODUCED, completed.result_oid)]
            unread = runtime.messages.unread(pid)
            assert unread[-1].sender == f"object_task:{task.task_id}"
            assert unread[-1].channel == runtime.config.object_tasks.notification_channel
            assert unread[-1].payload["status"] == ObjectTaskStatus.SUCCEEDED.value
            assert set(unread[-1].metadata["source_oids"]) == {owner.oid, completed.result_oid}
            assert {
                ref["oid"] for ref in unread[-1].metadata["data_flow_context"]["source_refs"]
            } == {owner.oid, completed.result_oid}
            lifecycle = [
                event.type
                for event in runtime.events.list(target=owner.oid)
                if event.type
                in {
                    EventType.OBJECT_TASK_STARTED,
                    EventType.OBJECT_TASK_RUNNING,
                    EventType.OBJECT_TASK_COMPLETED,
                }
            ]
            assert lifecycle == [
                EventType.OBJECT_TASK_STARTED,
                EventType.OBJECT_TASK_RUNNING,
                EventType.OBJECT_TASK_COMPLETED,
            ]
        finally:
            runtime.close()

    def test_object_task_notification_respects_recipient_identity_domain(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(
                image="base-agent:v0",
                goal="parent",
                authority_manifest={
                    "data_flow_policy": {
                        "schema_version": 1,
                        "allowed_tenants": ["tenant-a"],
                        "allowed_principals": [],
                    }
                },
            )
            _grant_process_spawn(runtime, parent)
            recipient = runtime.process.spawn_child(
                parent,
                "restricted notification recipient",
                authority_manifest={
                    "data_flow_policy": {
                        "schema_version": 1,
                        "allowed_tenants": [],
                        "allowed_principals": [],
                    }
                },
            )
            owner = runtime.memory.create_object(
                parent,
                ObjectType.ARTIFACT,
                {"name": "tenant owner"},
                metadata=ObjectMetadata(
                    title="tenant owner",
                    sensitivity="secret",
                    tenant="tenant-a",
                ),
                immutable=False,
            )
            grant_cap = runtime.capability.issue_trusted(
                subject=parent,
                resource="object:*",
                rights=[ObjectRight.GRANT.value],
                issued_by="test",
                uses_remaining=1,
            )

            task = runtime.object_tasks.start(
                parent,
                owner,
                "get_working_directory",
                {},
                notify_pid=recipient,
                grant_result_to_notify=True,
            )
            completed = runtime.object_tasks.wait(task.task_id, actor_pid=parent, timeout=2)

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert completed.result_oid is not None
            assert completed.notification.status == ObjectTaskNotificationStatus.FAILED
            assert "data_flow_policy" in (completed.notification.error or "")
            assert runtime.messages.unread(recipient) == []
            with pytest.raises(CapabilityDenied):
                runtime.memory.handle_for_oid(
                    recipient,
                    completed.result_oid,
                    required_rights={ObjectRight.READ.value},
                )
            assert runtime.store.get_capability(grant_cap.cap_id).uses_remaining == 1
        finally:
            runtime.close()

    def test_object_task_notify_result_rolls_back_grant_when_notification_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            _grant_process_spawn(runtime, parent)
            recipient = runtime.spawn_child_process(parent, "notify recipient")
            owner = _owner(runtime, parent)
            grant_cap = runtime.capability.issue_trusted(
                subject=parent,
                resource="object:*",
                rights=[ObjectRight.GRANT.value],
                issued_by="test",
                uses_remaining=1,
            )

            def fail_notification(*_args: object, **_kwargs: object) -> object:
                raise ProcessError("injected object task notification failure")

            monkeypatch.setattr(runtime.messages, "post", fail_notification)
            task = runtime.object_tasks.start(
                parent,
                owner,
                "get_working_directory",
                {},
                notify_pid=recipient,
                grant_result_to_notify=True,
            )

            completed = runtime.object_tasks.wait(
                task.task_id,
                actor_pid=parent,
                timeout=2,
            )

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert completed.result_oid is not None
            assert completed.notification.status == ObjectTaskNotificationStatus.FAILED
            assert "injected object task notification failure" in (
                completed.notification.error or ""
            )
            with pytest.raises(CapabilityDenied):
                runtime.memory.handle_for_oid(
                    recipient,
                    completed.result_oid,
                    required_rights={ObjectRight.READ.value},
                )
            assert runtime.store.get_capability(grant_cap.cap_id).uses_remaining == 1
        finally:
            runtime.close()

    def test_object_task_wait_includes_terminal_notification_delivery(self) -> None:
        runtime = Runtime.open("local")
        release_notification = threading.Event()
        notification_started = threading.Event()
        original_post = runtime.messages.post

        def delayed_post(*args: object, **kwargs: object) -> object:
            if (
                str(kwargs.get("sender") or "").startswith("object_task:")
                and kwargs.get("channel") == runtime.config.object_tasks.notification_channel
            ):
                notification_started.set()
                if not release_notification.wait(timeout=2):
                    raise TimeoutError("timed out waiting to release object task notification")
            return original_post(*args, **kwargs)

        runtime.messages.post = delayed_post  # type: ignore[method-assign]
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="object task wait notification")
            _grant_process_spawn(runtime, pid)
            owner = runtime.memory.create_object(
                pid,
                ObjectType.ARTIFACT,
                {"name": "owner"},
                metadata=ObjectMetadata(title="owner"),
                immutable=False,
            )
            task = runtime.object_tasks.start(pid, owner, "get_working_directory", {})

            with ThreadPoolExecutor(max_workers=1) as executor:
                waiter = executor.submit(runtime.object_tasks.wait, task.task_id, actor_pid=pid, timeout=2)
                assert notification_started.wait(timeout=2)
                time.sleep(0.05)
                assert not waiter.done()
                release_notification.set()
                completed = waiter.result(timeout=2)

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert completed.notification.status == ObjectTaskNotificationStatus.DELIVERED
        finally:
            release_notification.set()
            runtime.messages.post = original_post  # type: ignore[method-assign]
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

    def test_object_task_start_requires_process_spawn_authority(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="object task without spawn")
            owner = _owner(runtime, pid)
            before = len(runtime.process.list())

            with pytest.raises(CapabilityDenied, match="process:spawn"):
                runtime.object_tasks.start(pid, owner, "get_working_directory", {})

            assert len(runtime.process.list()) == before
            assert runtime.store.list_object_tasks(include_terminal=True) == []
        finally:
            runtime.close()

    def test_object_task_runner_does_not_inherit_external_capability_unless_explicit(self, tmp_path: Path) -> None:
        runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(tmp_path))
        try:
            path = f"agent_outputs/object_task_{uuid4().hex}.txt"
            pid = runtime.process.spawn(image="review-agent:v0", goal="write from object task")
            _grant_process_spawn(runtime, pid)
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
            _grant_process_spawn(runtime, pid)
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
            _grant_process_spawn(runtime, parent)
            child = runtime.spawn_child_process(parent, "object task creator")
            _grant_process_spawn(runtime, child)
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

    def test_concurrent_object_task_start_reserves_one_time_owner_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="one time owner race parent")
            _grant_process_spawn(runtime, parent)
            child = runtime.spawn_child_process(parent, "one time owner race creator")
            _grant_process_spawn(runtime, child)
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
            original_assert = runtime.object_tasks._assert_owner_rights
            authorized = threading.Barrier(2)

            def synchronized_assert(pid: str, handle: ObjectHandle, rights: set[str]) -> list[object]:
                decisions = original_assert(pid, handle, rights)
                authorized.wait(timeout=2)
                return decisions

            monkeypatch.setattr(runtime.object_tasks, "_assert_owner_rights", synchronized_assert)

            def start() -> object:
                return runtime.object_tasks.start(child, one_time_owner, "get_working_directory", {})

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(start) for _ in range(2)]
                outcomes: list[object] = []
                errors: list[BaseException] = []
                for future in futures:
                    try:
                        outcomes.append(future.result(timeout=2))
                    except BaseException as exc:
                        errors.append(exc)

            assert len(outcomes) == 1
            assert len(errors) == 1
            assert isinstance(errors[0], CapabilityDenied)
            completed = runtime.object_tasks.wait(outcomes[0].task_id, actor_pid=child, timeout=2)  # type: ignore[union-attr]
            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert runtime.store.get_capability(cap.cap_id).uses_remaining == 0
            assert len(runtime.store.list_object_tasks(include_terminal=True)) == 1
        finally:
            runtime.close()

    def test_object_task_start_cleans_runner_before_restoring_one_time_owner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="one time owner rollback parent")
            _grant_process_spawn(runtime, parent)
            child = runtime.spawn_child_process(parent, "object task creator")
            _grant_process_spawn(runtime, child)
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
            runner_pids: list[str] = []

            def fail_insert(task: object) -> None:
                runner_pids.append(str(getattr(task, "runner_pid")))
                raise RuntimeError("object task insert failed")

            monkeypatch.setattr(runtime.store, "insert_object_task", fail_insert)

            with pytest.raises(RuntimeError, match="object task insert failed"):
                runtime.object_tasks.start(child, one_time_owner, "get_working_directory", {})

            assert len(runner_pids) == 1
            assert runtime.store.get_process(runner_pids[0]) is None
            assert runtime.capability.list_subject(runner_pids[0], include_inactive=True) == []
            assert runtime.store.list_object_tasks(include_terminal=True) == []
            assert runtime.store.get_capability(cap.cap_id).uses_remaining == 1
        finally:
            runtime.close()

    def test_object_task_schedule_failure_terminalizes_task_and_removes_runner(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="schedule failure parent")
            _grant_process_spawn(runtime, parent)
            child = runtime.spawn_child_process(parent, "schedule failure creator")
            _grant_process_spawn(runtime, child)
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

            def fail_schedule(_task_id: str) -> object:
                raise RuntimeError("executor rejected object task")

            monkeypatch.setattr(runtime.object_tasks, "_schedule_task_locked", fail_schedule)

            with pytest.raises(RuntimeError, match="executor rejected object task"):
                runtime.object_tasks.start(child, one_time_owner, "get_working_directory", {})

            tasks = runtime.store.list_object_tasks(include_terminal=True)
            assert len(tasks) == 1
            assert tasks[0].status == ObjectTaskStatus.FAILED
            assert runtime.store.get_process(str(tasks[0].runner_pid)) is None
            assert runtime.store.get_capability(cap.cap_id).uses_remaining == 0
            assert runtime.store.list_object_tasks(include_terminal=False) == []
        finally:
            runtime.close()

    def test_object_task_result_wiring_failure_removes_result_and_terminalizes_runner(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="result wiring failure")
            _grant_process_spawn(runtime, pid)
            owner = _owner(runtime, pid)
            result_oids: list[str] = []

            def fail_link(
                _actor: str,
                _src_oid: str,
                _relation: object,
                dst_oid: str,
                **_kwargs: object,
            ) -> object:
                result_oids.append(dst_oid)
                raise RuntimeError("result link failed")

            monkeypatch.setattr(runtime.memory, "link_objects_trusted", fail_link)
            task = runtime.object_tasks.start(pid, owner, "get_working_directory", {})
            failed = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)

            assert failed.status == ObjectTaskStatus.FAILED
            assert "result link failed" in (failed.error or "")
            assert len(result_oids) == 1
            assert runtime.store.get_object(result_oids[0]) is None
            runner = runtime.store.get_process(str(task.runner_pid))
            assert runner is not None
            assert runner.status in runtime.process.TERMINAL_STATUSES
            creator = runtime.process.get(pid)
            assert creator.memory_view is None or all(root.oid != result_oids[0] for root in creator.memory_view.roots)
            assert runtime.store.list_objects_owned_by(ObjectOwnerKind.OBJECT_TASK, task.task_id) == []
        finally:
            runtime.close()

    def test_object_task_success_commit_failure_discards_result_and_fails_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="success commit failure")
            _grant_process_spawn(runtime, pid)
            owner = _owner(runtime, pid)
            original_update = runtime.store.update_object_task
            failed_success_commit = False

            def fail_success_once(task: object) -> None:
                nonlocal failed_success_commit
                if getattr(task, "status", None) == ObjectTaskStatus.SUCCEEDED and not failed_success_commit:
                    failed_success_commit = True
                    raise RuntimeError("object task success commit failed")
                original_update(task)

            monkeypatch.setattr(runtime.store, "update_object_task", fail_success_once)
            task = runtime.object_tasks.start(pid, owner, "get_working_directory", {})
            failed = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)

            assert failed_success_commit
            assert failed.status == ObjectTaskStatus.FAILED
            assert "object task success commit failed" in (failed.error or "")
            assert runtime.store.list_objects_owned_by(ObjectOwnerKind.OBJECT_TASK, task.task_id) == []
            runner = runtime.store.get_process(str(task.runner_pid))
            assert runner is not None
            assert runner.status in runtime.process.TERMINAL_STATUSES
            creator = runtime.process.get(pid)
            assert creator.memory_view is None or all(
                runtime.store.get_object(root.oid) is not None for root in creator.memory_view.roots
            )
        finally:
            runtime.close()

    def test_object_task_cancel_winning_during_result_wiring_discards_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open("local")
        release_link = threading.Event()
        link_started = threading.Event()
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="cancel during result wiring")
            _grant_process_spawn(runtime, pid)
            owner = _owner(runtime, pid)
            original_link = runtime.memory.link_objects_trusted
            result_oids: list[str] = []

            def delayed_link(
                actor: str,
                src_oid: str,
                relation: object,
                dst_oid: str,
                **kwargs: object,
            ) -> None:
                result_oids.append(dst_oid)
                link_started.set()
                assert release_link.wait(timeout=2)
                original_link(actor, src_oid, relation, dst_oid, **kwargs)  # type: ignore[arg-type]

            monkeypatch.setattr(runtime.memory, "link_objects_trusted", delayed_link)
            task = runtime.object_tasks.start(pid, owner, "get_working_directory", {})
            assert link_started.wait(timeout=2)

            cancelled = runtime.object_tasks.cancel(task.task_id, actor_pid=pid, reason="cancel won")
            assert cancelled.status == ObjectTaskStatus.CANCELLED
            release_link.set()
            settled = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)

            assert settled.status == ObjectTaskStatus.CANCELLED
            assert len(result_oids) == 1
            assert runtime.store.get_object(result_oids[0]) is None
            assert runtime.store.list_objects_owned_by(ObjectOwnerKind.OBJECT_TASK, task.task_id) == []
            creator = runtime.process.get(pid)
            assert creator.memory_view is None or all(root.oid != result_oids[0] for root in creator.memory_view.roots)
        finally:
            release_link.set()
            runtime.close()

    def test_object_task_notification_can_interrupt_and_wake_message_waiter(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            _grant_process_spawn(runtime, parent)
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
            _grant_process_spawn(runtime, parent)
            _grant_delegable_clock_sleep(runtime, parent)
            child = runtime.spawn_child_process(parent, "notify me")
            owner = _owner(runtime, parent)
            task = runtime.object_tasks.start(
                parent,
                owner,
                "sleep",
                {"seconds": 0.05},
                notify_pid=child,
                inherit_capabilities=_inherit_clock_sleep(),
            )
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
            _grant_process_spawn(runtime, parent)
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

    def test_object_task_notify_result_consumes_one_time_grant_authority(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            _grant_process_spawn(runtime, parent)
            recipient = runtime.spawn_child_process(parent, "notify recipient")
            owner = _owner(runtime, parent)
            grant_cap = runtime.capability.issue_trusted(
                subject=parent,
                resource="object:*",
                rights=[ObjectRight.GRANT.value],
                issued_by="test",
                uses_remaining=1,
            )
            first = runtime.object_tasks.start(
                parent,
                owner,
                "get_working_directory",
                {},
                notify_pid=recipient,
                grant_result_to_notify=True,
            )
            first_completed = runtime.object_tasks.wait(first.task_id, actor_pid=parent, timeout=2)
            assert first_completed.status == ObjectTaskStatus.SUCCEEDED
            assert first_completed.result_oid is not None
            recipient_handle = runtime.memory.handle_for_oid(
                recipient,
                first_completed.result_oid,
                required_rights={ObjectRight.READ.value},
            )
            assert recipient_handle.oid == first_completed.result_oid
            assert runtime.store.get_capability(grant_cap.cap_id).uses_remaining == 0

            second = runtime.object_tasks.start(
                parent,
                owner,
                "get_working_directory",
                {},
                notify_pid=recipient,
                grant_result_to_notify=True,
            )
            second_completed = runtime.object_tasks.wait(second.task_id, actor_pid=parent, timeout=2)

            assert second_completed.status == ObjectTaskStatus.FAILED
            assert "cannot grant object task result" in (second_completed.error or "")
            notify_object_grants = [
                cap
                for cap in runtime.capability.list_subject(recipient)
                if cap.resource.startswith("object:")
            ]
            assert [
                cap.resource
                for cap in notify_object_grants
                if cap.issued_by == f"object_task:{first.task_id}"
            ] == [f"object:{first_completed.result_oid}"]
            assert not any(cap.issued_by == f"object_task:{second.task_id}" for cap in notify_object_grants)
            reserve_record = next(
                record
                for record in runtime.audit.trace()
                if record.action == "capability.reserve_use" and grant_cap.cap_id in record.capability_refs
            )
            assert any(
                record.action == "capability.commit_reserved_use"
                and record.target == f"capability_reservation:{reserve_record.decision['reservation_id']}"
                for record in runtime.audit.trace()
            )
            assert runtime.store.list_objects_owned_by(ObjectOwnerKind.OBJECT_TASK, second.task_id) == []
        finally:
            runtime.close()

    def test_object_task_waiting_message_state_does_not_repeat_tool_call(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="wait task")
            _grant_process_spawn(runtime, pid)
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

    def test_posted_process_message_resumes_waiting_object_task_runner(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="message resume task")
            _grant_process_spawn(runtime, pid)
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(
                pid,
                owner,
                "receive_process_messages",
                {"channel": "resume-me"},
            )
            waiting = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)
            assert waiting.status == ObjectTaskStatus.WAITING_MESSAGE

            runtime.messages.post(
                sender=pid,
                recipient_pid=str(waiting.runner_pid),
                channel="resume-me",
                subject="ready",
                body="continue",
            )
            completed = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert any(record.action == "object_task.owner_watch.resume" for record in runtime.audit.trace())
        finally:
            runtime.close()

    def test_posted_process_message_only_resumes_recipient_object_task_runner(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="message recipient isolation")
            _grant_process_spawn(runtime, pid)
            first_owner = _owner(runtime, pid)
            second_owner = _owner(runtime, pid)
            first = runtime.object_tasks.start(
                pid,
                first_owner,
                "receive_process_messages",
                {"channel": "shared-resume"},
            )
            second = runtime.object_tasks.start(
                pid,
                second_owner,
                "receive_process_messages",
                {"channel": "shared-resume"},
            )
            first_waiting = runtime.object_tasks.wait(first.task_id, actor_pid=pid, timeout=2)
            second_waiting = runtime.object_tasks.wait(second.task_id, actor_pid=pid, timeout=2)
            assert first_waiting.status == ObjectTaskStatus.WAITING_MESSAGE
            assert second_waiting.status == ObjectTaskStatus.WAITING_MESSAGE
            before_resumes = len([record for record in runtime.audit.trace() if record.action == "object_task.owner_watch.resume"])

            runtime.messages.post(
                sender=pid,
                recipient_pid=str(first_waiting.runner_pid),
                channel="shared-resume",
                subject="ready",
                body="continue",
            )
            first_completed = runtime.object_tasks.wait(first.task_id, actor_pid=pid, timeout=2)
            second_still_waiting = runtime.object_tasks.wait(second.task_id, actor_pid=pid, timeout=0.05)
            after_resumes = len([record for record in runtime.audit.trace() if record.action == "object_task.owner_watch.resume"])

            assert first_completed.status == ObjectTaskStatus.SUCCEEDED
            assert second_still_waiting.status == ObjectTaskStatus.WAITING_MESSAGE
            assert after_resumes - before_resumes == 1
            assert len([record for record in runtime.audit.trace() if record.action == "tool.call_waiting_message"]) == 2
        finally:
            runtime.close()

    def test_child_process_exit_resumes_waiting_object_task_runner(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="process resume task")
            _grant_process_spawn(runtime, pid)
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(
                pid,
                owner,
                "receive_process_messages",
                {"channel": "never"},
            )
            waiting = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)
            assert waiting.status == ObjectTaskStatus.WAITING_MESSAGE
            runner_pid = str(waiting.runner_pid)
            _grant_process_spawn(runtime, runner_pid)
            child_pid = runtime.spawn_child_process(runner_pid, "child waited by object task")
            runtime.tools.configure_process_tools(runner_pid, ["wait_child_process"], assigned_by="test")
            runtime.object_tasks._pending_args[task.task_id] = {"child_pid": child_pid}
            runtime.store.update_object_task(
                replace(
                    waiting,
                    status=ObjectTaskStatus.WAITING_PROCESS,
                    tool="wait_child_process",
                    wait={"child_pid": child_pid},
                )
            )

            runtime.process.exit(child_pid, message="child done")
            completed = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert any(record.action == "object_task.process_resume" for record in runtime.audit.trace())
        finally:
            runtime.close()

    def test_object_task_request_permission_resumes_after_human_decision(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(
                image="base-agent:v0",
                goal="permission task",
                authority_manifest={
                    "authorized_capabilities": [
                        {
                            "resource": "human:owner",
                            "rights": [CapabilityRight.WRITE.value],
                            "delegable": True,
                        }
                    ],
                    "approval_policy": {
                        "requestable_capabilities": [
                            {
                                "resource": "filesystem:workspace:agent_outputs/*",
                                "rights": [CapabilityRight.WRITE.value],
                            }
                        ]
                    },
                },
            )
            _grant_process_spawn(runtime, pid)
            owner = _owner(runtime, pid)
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
            _grant_process_spawn(runtime, pid)
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
            assert message["metadata"]["source_oids"] == [owner.oid]
            assert [
                ref["oid"] for ref in message["metadata"]["data_flow_context"]["source_refs"]
            ] == [owner.oid]
            assert any(record.action == "object_task.owner_watch.resume" for record in runtime.audit.trace())
        finally:
            runtime.close()

    def test_object_task_owner_watch_does_not_replay_unsafe_waiting_tool(self) -> None:
        runtime = Runtime.open("local")
        try:
            counter: dict[str, int] = {}
            handle = runtime.tools.register_tool(SideEffectThenWaitTool(counter), registered_by="test", ephemeral=True)
            pid = runtime.process.spawn(image="base-agent:v0", goal="unsafe replay")
            _grant_process_spawn(runtime, pid)
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
            _grant_process_spawn(runtime, parent)
            child = runtime.spawn_child_process(parent, "watcher")
            _grant_process_spawn(runtime, child)
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
            _grant_process_spawn(runtime, pid)
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
            _grant_process_spawn(runtime, pid)
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
            _grant_process_spawn(runtime, pid)
            _grant_delegable_clock_sleep(runtime, pid)
            owner = _owner(runtime, pid)
            result = runtime.memory.create_object(
                pid,
                ObjectType.SUMMARY,
                {"kept": True},
                metadata=ObjectMetadata(title="result"),
            )
            task = runtime.object_tasks.start(
                pid,
                owner,
                "sleep",
                {"seconds": 0.05},
                inherit_capabilities=_inherit_clock_sleep(),
            )
            runtime.process.exit(pid, result=result, message="creator exited")

            assert runtime.store.get_object(owner.oid) is not None
            assert runtime.store.get_object(result.oid) is not None
            completed = runtime.object_tasks.wait(task.task_id, timeout=2)

            assert completed.status == ObjectTaskStatus.SUCCEEDED
            assert runtime.store.get_object(owner.oid) is None
            assert runtime.store.get_object(result.oid) is not None
            assert runtime.store.get_object(result.oid).owner_kind == ObjectOwnerKind.PROCESS_RESULT
            assert completed.notification.status == ObjectTaskNotificationStatus.UNDELIVERED_TERMINAL
        finally:
            runtime.close()

    def test_object_task_cancel_updates_task_and_runner_process(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="cancel task")
            _grant_process_spawn(runtime, pid)
            _grant_delegable_clock_sleep(runtime, pid)
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(
                pid,
                owner,
                "sleep",
                {"seconds": 1.0},
                inherit_capabilities=_inherit_clock_sleep(),
            )

            cancelled = runtime.object_tasks.cancel(task.task_id, actor_pid=pid, reason="no longer needed")

            assert cancelled.status == ObjectTaskStatus.CANCELLED
            assert runtime.process.get(str(cancelled.runner_pid)).status == ProcessStatus.KILLED
        finally:
            runtime.close()

    def test_object_task_cancel_cannot_resurrect_runner_during_running_transition(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open("local")
        transition_started = threading.Event()
        release_transition = threading.Event()
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="cancel during running transition")
            _grant_process_spawn(runtime, pid)
            _grant_delegable_clock_sleep(runtime, pid)
            owner = _owner(runtime, pid)
            original_set_runner_status = runtime.object_tasks._set_runner_status

            def delayed_set_runner_status(
                runner_pid: str,
                status: ProcessStatus,
                message: str | None = None,
            ) -> None:
                transition_started.set()
                assert release_transition.wait(timeout=2)
                original_set_runner_status(runner_pid, status, message)

            monkeypatch.setattr(runtime.object_tasks, "_set_runner_status", delayed_set_runner_status)
            task = runtime.object_tasks.start(
                pid,
                owner,
                "sleep",
                {"seconds": 1.0},
                inherit_capabilities=_inherit_clock_sleep(),
            )
            assert transition_started.wait(timeout=2)

            cancel_started = threading.Event()

            def cancel() -> ObjectTask:
                cancel_started.set()
                return runtime.object_tasks.cancel(task.task_id, actor_pid=pid, reason="cancel won")

            with ThreadPoolExecutor(max_workers=1) as executor:
                cancel_future = executor.submit(cancel)
                assert cancel_started.wait(timeout=2)
                assert not cancel_future.done()
                release_transition.set()
                cancelled = cancel_future.result(timeout=2)

            assert cancelled.status == ObjectTaskStatus.CANCELLED
            runner_pid = str(cancelled.runner_pid)
            assert runtime.process.get(runner_pid).status == ProcessStatus.KILLED
            settled = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)
            assert settled.status == ObjectTaskStatus.CANCELLED
            assert runtime.process.get(runner_pid).status == ProcessStatus.KILLED
        finally:
            release_transition.set()
            runtime.close()

    def test_object_task_refuses_to_cancel_running_sync_side_effect_tool(self) -> None:
        runtime = Runtime.open("local")
        try:
            handle = runtime.tools.register_tool(SlowSyncSideEffectTool(), registered_by="test", ephemeral=True)
            pid = runtime.process.spawn(image="base-agent:v0", goal="sync cancel")
            _grant_process_spawn(runtime, pid)
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
            _grant_process_spawn(runtime, pid)
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(pid, owner, "receive_process_messages", {"channel": "never"})
            waiting = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)
            assert waiting.status == ObjectTaskStatus.WAITING_MESSAGE

            runtime.process.signal(str(waiting.runner_pid), "cancel", {"reason": "external kill"})
            refreshed = runtime.object_tasks.get(task.task_id, actor_pid=pid)

            assert refreshed.status == ObjectTaskStatus.CANCELLED
            assert refreshed.error is not None and refreshed.error.startswith("result_oid:")
            reason = runtime.store.get_object(refreshed.error.split(":", 1)[1])
            assert reason is not None
            assert reason.payload == {"reason": "external kill"}
        finally:
            runtime.close()

    def test_object_task_list_limit_applies_after_visibility_filter(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            _grant_process_spawn(runtime, parent)
            child = runtime.spawn_child_process(parent, "child")
            _grant_process_spawn(runtime, child)
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

    def test_object_task_rejects_invalid_list_limit_and_wait_timeout(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="invalid object task bounds")
            _grant_process_spawn(runtime, pid)
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(pid, owner, "receive_process_messages", {"channel": "never"})

            with pytest.raises(ValidationError, match="list limit"):
                runtime.object_tasks.list(actor_pid=pid, limit=True)
            with pytest.raises(ValidationError, match="list limit"):
                runtime.object_tasks.list(actor_pid=pid, limit=-1)
            with pytest.raises(ValidationError, match="wait timeout"):
                runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=float("nan"))
        finally:
            runtime.close()

    def test_runtime_shutdown_drains_object_task_wait_transition_before_store_close(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="shutdown object task wait")
            _grant_process_spawn(runtime, pid)
            owner = _owner(runtime, pid)
            runtime.object_tasks.start(pid, owner, "receive_process_messages", {"channel": "never"})

            result = runtime.shutdown(actor="test", reason="object-task-wait-drain")

            assert result["ok"] is True
        finally:
            runtime.close()

    def test_runtime_shutdown_keeps_store_open_when_object_task_executor_is_still_running(self) -> None:
        config = replace(
            DEFAULT_CONFIG,
            object_tasks=replace(DEFAULT_CONFIG.object_tasks, shutdown_join_timeout_s=0.01),
        )
        runtime = Runtime.open("local", config=config)
        try:
            handle = runtime.tools.register_tool(SlowSyncSideEffectTool(), registered_by="test", ephemeral=True)
            pid = runtime.process.spawn(image="base-agent:v0", goal="shutdown slow object task")
            _grant_process_spawn(runtime, pid)
            runtime.tools.configure_process_tools(pid, [handle], assigned_by="test")
            owner = _owner(runtime, pid)
            task = runtime.object_tasks.start(pid, owner, "slow_sync_side_effect_for_object_task", {})
            deadline = time.monotonic() + 1.0
            while runtime.object_tasks.get(task.task_id, actor_pid=pid).status != ObjectTaskStatus.RUNNING:
                if time.monotonic() >= deadline:
                    pytest.fail("object task did not enter running state")
                time.sleep(0.01)

            result = runtime.shutdown(actor="test", reason="object-task-slow-drain")

            assert result["ok"] is False
            assert result["object_tasks_stopped"] is False
            assert runtime.store.get_object_task(task.task_id) is not None

            time.sleep(0.3)
            retry = runtime.shutdown(actor="test", reason="object-task-slow-drain-retry")
            assert retry["ok"] is True
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
            _grant_process_spawn(runtime, pid)
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

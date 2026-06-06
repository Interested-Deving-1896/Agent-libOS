from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from agent_libos import Runtime
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import CapabilityRight, ProcessStatus, ResourceBudget
from scripts.llm_context_probe import last_tool_result, static_prefix


class ChildProcessToolTests(unittest.TestCase):
    def test_fork_wait_tool_blocks_parent_until_child_exits_and_exposes_result(self) -> None:
        runtime = Runtime.open("local")
        try:
            # This test needs the parent to reach wait_child_process before
            # the newly forked child gets a scheduler task.
            runtime.scheduler.poll_interval_s = 1.0
            client = ParentChildClient()
            runtime.llm.client = client
            parent = runtime.process.spawn(image="base-agent:v0", goal="fork child and wait")

            results = asyncio.run(runtime.arun_until_idle(max_quanta=8))

            self.assertEqual(runtime.process.get(parent).status, ProcessStatus.EXITED)
            self.assertIsNotNone(client.child_pid)
            assert client.child_pid is not None
            self.assertEqual(runtime.process.get(client.child_pid).status, ProcessStatus.EXITED)
            self.assertTrue(any(isinstance(result, dict) and result.get("waiting_event") for result in results))

            wait_result = next(result for result in results if _action_name(result) == "wait_child_process")
            result_oid = wait_result["result"]["payload"]["result_oid"]
            child_result = runtime.store.get_object(result_oid)
            self.assertIsNotNone(child_result)
            assert child_result is not None
            self.assertEqual(child_result.payload["value"], 42)

            parent_view = runtime.process.get(parent).memory_view
            self.assertIsNotNone(parent_view)
            assert parent_view is not None
            self.assertIn(result_oid, [handle.oid for handle in parent_view.roots])
            self.assertIn("process.wait_wake", [record.action for record in runtime.audit.trace()])
        finally:
            runtime.close()

    def test_child_list_signal_and_budget_are_enforced(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(
                image="base-agent:v0",
                goal="manage one child",
                resource_budget=ResourceBudget(max_child_processes=1),
            )
            other = runtime.process.spawn(image="base-agent:v0", goal="not a child")

            forked = runtime.tools.call(parent, "fork_child_process", {"goal": "child", "include_parent_roots": False})
            self.assertTrue(forked.ok, forked.error)
            child = forked.payload["child_pid"]

            listed = runtime.tools.call(parent, "list_child_processes", {})
            self.assertTrue(listed.ok, listed.error)
            self.assertEqual([entry["pid"] for entry in listed.payload["children"]], [child])
            self.assertEqual(listed.payload["children"][0]["working_directory"], ".")

            paused = runtime.tools.call(parent, "signal_child_process", {"child_pid": child, "signal": "pause"})
            self.assertTrue(paused.ok, paused.error)
            self.assertEqual(paused.payload["status"], "paused")

            resumed = runtime.tools.call(parent, "signal_child_process", {"child_pid": child, "signal": "resume"})
            self.assertTrue(resumed.ok, resumed.error)
            self.assertEqual(resumed.payload["status"], "runnable")

            denied_signal = runtime.tools.call(parent, "signal_child_process", {"child_pid": other, "signal": "pause"})
            self.assertFalse(denied_signal.ok)
            self.assertIn("not a child", denied_signal.error or "")

            denied_fork = runtime.tools.call(parent, "fork_child_process", {"goal": "second child"})
            self.assertFalse(denied_fork.ok)
            self.assertIn("exhausted child process budget", denied_fork.error or "")
        finally:
            runtime.close()

    def test_spawn_child_process_creates_fresh_child_without_parent_memory_or_default_caps(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="review-agent:v0", goal="parent")
            parent_note = runtime.memory.create_object(
                pid=parent,
                object_type="observation",
                name="parent.note",
                payload={"visible_to_parent": True},
            )

            spawned = runtime.tools.call(
                parent,
                "spawn_child_process",
                {"goal": "fresh child", "image": "coding-agent:v0"},
            )

            self.assertTrue(spawned.ok, spawned.error)
            child = runtime.process.get(spawned.payload["child_pid"])
            self.assertEqual(child.parent_pid, parent)
            self.assertEqual(child.image_id, "coding-agent:v0")
            self.assertIn("read_text_file", child.tool_table)
            self.assertNotIn(parent_note.oid, [handle.oid for handle in child.memory_view.roots])
            self.assertEqual([handle.oid for handle in child.memory_view.roots], [child.goal_oid])
            read_resource = runtime.filesystem.resource_for_path("README.md")
            self.assertFalse(runtime.capability.check(child.pid, read_resource, CapabilityRight.READ))
        finally:
            runtime.close()

    def test_spawn_child_process_inherits_only_explicit_capabilities(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="review-agent:v0", goal="parent")
            runtime.filesystem.grant_path(parent, "README.md", [CapabilityRight.READ], issued_by="test")

            spawned = runtime.tools.call(
                parent,
                "spawn_child_process",
                {"goal": "read one file", "inherit_read_files": ["README.md"]},
            )

            self.assertTrue(spawned.ok, spawned.error)
            child = runtime.process.get(spawned.payload["child_pid"])
            allowed = runtime.filesystem.resource_for_path("README.md")
            other = runtime.filesystem.resource_for_path("pyproject.toml")
            self.assertTrue(runtime.capability.check(child.pid, allowed, CapabilityRight.READ))
            self.assertFalse(runtime.capability.check(child.pid, other, CapabilityRight.READ))
        finally:
            runtime.close()

    def test_exec_process_swaps_image_without_granting_target_image_capabilities(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="become coding agent")
            runtime.filesystem.grant_workspace(pid, [CapabilityRight.READ], issued_by="test")

            executed = runtime.tools.call(
                pid,
                "exec_process",
                {
                    "image": "coding-agent:v0",
                    "goal": "inspect without automatic capability lift",
                    "preserve_capabilities": False,
                    "preserve_memory": False,
                },
            )

            self.assertTrue(executed.ok, executed.error)
            process = runtime.process.get(pid)
            self.assertEqual(process.image_id, "coding-agent:v0")
            self.assertIn("read_text_file", process.tool_table)
            self.assertIn("spawn_child_process", process.tool_table)
            read_resource = runtime.filesystem.resource_for_path("README.md")
            self.assertFalse(runtime.capability.check(pid, read_resource, CapabilityRight.READ))
            self.assertEqual([handle.oid for handle in process.memory_view.roots], [process.goal_oid])
            self.assertIn("process.exec", [record.action for record in runtime.audit.trace()])
        finally:
            runtime.close()

    def test_merge_child_memory_tool_adds_child_view_objects_to_parent(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="merge child")
            child = runtime.process.fork(parent, goal="produce result")
            created = runtime.tools.call(
                child,
                "create_memory_object",
                {"name": "child.result", "type": "summary", "payload": {"merged": True}},
            )
            self.assertTrue(created.ok, created.error)
            result_oid = created.payload["oid"]
            runtime.tools.call(child, "process_exit", {"result_oid": result_oid})

            merged = runtime.tools.call(parent, "merge_child_memory", {"child_pid": child})

            self.assertTrue(merged.ok, merged.error)
            self.assertIn(result_oid, merged.payload["merged_oids"])
            parent_view = runtime.process.get(parent).memory_view
            self.assertIsNotNone(parent_view)
            assert parent_view is not None
            self.assertIn(result_oid, [handle.oid for handle in parent_view.roots])
        finally:
            runtime.close()

    def test_fork_does_not_resurrect_revoked_image_default_capability(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="coding-agent:v0", goal="fork after revoke")
            path = "README.md"
            for cap in list(runtime.capability.capabilities_for(parent)):
                if cap.resource == "filesystem:workspace:*" and CapabilityRight.READ.value in cap.rights:
                    runtime.capability.revoke(cap.cap_id, revoked_by="cli", reason="revoked before fork")

            forked = runtime.tools.call(parent, "fork_child_process", {"goal": "try reading"})
            self.assertTrue(forked.ok, forked.error)
            child = forked.payload["child_pid"]
            denied = runtime.tools.call(child, "read_text_file", {"path": path})

            self.assertFalse(denied.ok)
            self.assertIn("lacks read", denied.error or "")
        finally:
            runtime.close()


class ParentChildClient:
    def __init__(self) -> None:
        self.parent_pid: str | None = None
        self.child_pid: str | None = None
        self.parent_step = 0
        self.calls = 0

    async def acomplete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        # Keep this test focused on child wait/resume semantics. The generic
        # sync-client path runs in a worker thread, which can let the scheduler
        # start the child before the parent has issued wait_child_process.
        return self.complete_action(messages, tools)

    def complete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        self.calls += 1
        pid = _pid_from_messages(messages)
        parent_pid = _parent_pid_from_messages(messages)
        if parent_pid is not None:
            return self._completion(
                "process_exit",
                {"payload": {"child_pid": pid, "value": 42}},
            )
        self.parent_pid = pid
        if self.parent_step == 0:
            self.parent_step = 1
            return self._completion(
                "fork_child_process",
                {"goal": "return value 42", "mode": "worker", "include_parent_roots": False},
            )
        if self.parent_step == 1:
            self.child_pid = _last_tool_result(messages, "fork_child_process")["child_pid"]
            self.parent_step = 2
            return self._completion("wait_child_process", {"child_pid": self.child_pid})
        if self.parent_step == 2:
            wait_result = _last_tool_result(messages, "wait_child_process")
            self.parent_step = 3
            return self._completion(
                "process_exit",
                {"payload": {"waited": wait_result["ready"], "child_pid": wait_result["child_pid"]}},
            )
        raise AssertionError("parent action plan is complete")

    def _completion(self, name: str, args: dict[str, Any]) -> LLMCompletion:
        return LLMCompletion(
            content="",
            tool_calls=[{"id": f"child_process_{self.calls}", "name": name, "arguments": json.dumps(args)}],
        )


def _pid_from_messages(messages: list[dict[str, str]]) -> str:
    pid = static_prefix(messages).get("pid")
    if not isinstance(pid, str) or not pid:
        raise AssertionError("prompt did not include process pid")
    return pid


def _parent_pid_from_messages(messages: list[dict[str, str]]) -> str | None:
    value = static_prefix(messages).get("parent_pid")
    if value is None or isinstance(value, str):
        return value
    raise AssertionError("prompt parent pid had an unexpected shape")


def _last_tool_result(messages: list[dict[str, str]], tool_name: str) -> dict[str, Any]:
    result = last_tool_result(messages, tool_name)
    if result is not None:
        return result
    raise AssertionError(f"no visible result for {tool_name}")


def _action_name(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    action = result.get("action")
    if isinstance(action, dict):
        return action.get("action")
    return None


if __name__ == "__main__":
    unittest.main()

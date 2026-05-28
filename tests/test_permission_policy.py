from __future__ import annotations

import hashlib
import json
import unittest
from uuid import uuid4

from agent_libos import Runtime
from agent_libos.capability.manager import CapabilityManager
from agent_libos.exceptions import HumanApprovalRequired
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import CapabilityRight, HumanRequestStatus, ProcessStatus


class PermissionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Runtime.open("local")
        self.human_output: list[str] = []
        self.runtime.human.output_sink = self.human_output.append

    def tearDown(self) -> None:
        self.runtime.close()

    def test_request_permission_tool_can_set_always_allow_policy(self) -> None:
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="request write")
        path = self._path()
        resource = self.runtime.filesystem.resource_for(path)

        request = self.runtime.tools.call(
            pid,
            "request_permission",
            {"resource": resource, "rights": ["write"], "reason": "write summary"},
        )
        processed = self.runtime.human.drain_terminal_queue(auto_policy=CapabilityManager.ALWAYS_ALLOW)
        allowed = self.runtime.tools.call(pid, "write_text_file", {"path": path, "content": "allowed"})

        self.assertTrue(request.ok)
        self.assertEqual(processed[0].status, HumanRequestStatus.APPROVED)
        self.assertEqual(self.runtime.process.get(pid).status, ProcessStatus.RUNNABLE)
        self.assertEqual(
            self.runtime.capability.permission_policy(pid, resource, CapabilityRight.WRITE),
            CapabilityManager.ALWAYS_ALLOW,
        )
        self.assertTrue(allowed.ok)
        self.assertEqual((self.runtime.workspace_root / path).read_text(encoding="utf-8"), "allowed")

    def test_request_permission_tool_can_set_always_deny_policy_and_resume_process(self) -> None:
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="request denied write")
        path = self._path()
        resource = self.runtime.filesystem.resource_for(path)

        self.runtime.tools.call(
            pid,
            "request_permission",
            {"resource": resource, "rights": ["write"], "reason": "write summary"},
        )
        processed = self.runtime.human.drain_terminal_queue(auto_policy=CapabilityManager.ALWAYS_DENY)
        denied = self.runtime.tools.call(pid, "write_text_file", {"path": path, "content": "denied"})

        self.assertEqual(processed[0].status, HumanRequestStatus.REJECTED)
        self.assertEqual(self.runtime.process.get(pid).status, ProcessStatus.RUNNABLE)
        self.assertEqual(
            self.runtime.capability.permission_policy(pid, resource, CapabilityRight.WRITE),
            CapabilityManager.ALWAYS_DENY,
        )
        self.assertFalse(denied.ok)
        self.assertIn("denied write", denied.error or "")
        self.assertFalse((self.runtime.workspace_root / path).exists())

    def test_ask_each_time_prompts_from_filesystem_primitive_and_consumes_one_time_grant(self) -> None:
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="ask every write")
        path = self._path()
        resource = self.runtime.filesystem.resource_for(path)
        self.runtime.capability.set_permission_policy(
            subject=pid,
            resource=resource,
            rights=[CapabilityRight.WRITE],
            policy=CapabilityManager.ASK_EACH_TIME,
            issued_by="test",
        )

        with self.assertRaises(HumanApprovalRequired):
            self.runtime.tools.call(pid, "write_text_file", {"path": path, "content": "first"})
        pending = self.runtime.human.pending()[0]
        context = pending.payload["context"]
        first_prompt = self.runtime.human.drain_terminal_queue(auto_approve=True)
        retry = self.runtime.tools.call(pid, "write_text_file", {"path": path, "content": "first"})
        with self.assertRaises(HumanApprovalRequired):
            self.runtime.tools.call(pid, "write_text_file", {"path": path, "content": "second"})

        self.assertEqual(context["primitive"], "runtime.filesystem.write_text")
        self.assertEqual(context["path"], path)
        self.assertEqual(context["resource"], resource)
        self.assertEqual(context["grant_scope"], "one_time")
        self.assertEqual(context["content_bytes"], 5)
        self.assertEqual(context["content_preview"], "first")
        self.assertEqual(context["content_sha256"], hashlib.sha256(b"first").hexdigest())
        self.assertEqual(context["target"]["exists"], False)
        self.assertEqual(first_prompt[0].payload["type"], "external_operation_approval")
        self.assertEqual(first_prompt[0].status, HumanRequestStatus.APPROVED)
        self.assertIn("content sha256", self.human_output[0])
        self.assertIn("content preview", self.human_output[0])
        self.assertIn("one-time capability", self.human_output[0])
        self.assertTrue(retry.ok)
        self.assertEqual((self.runtime.workspace_root / path).read_text(encoding="utf-8"), "first")
        self.assertEqual(self.runtime.process.get(pid).status, ProcessStatus.WAITING_HUMAN)
        self.assertEqual(
            self.runtime.capability.permission_policy(pid, resource, CapabilityRight.WRITE),
            CapabilityManager.ASK_EACH_TIME,
        )

    def test_rejected_per_use_prompt_resumes_process_without_writing(self) -> None:
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="reject one write")
        path = self._path()
        resource = self.runtime.filesystem.resource_for(path)
        self.runtime.capability.set_permission_policy(
            subject=pid,
            resource=resource,
            rights=[CapabilityRight.WRITE],
            policy=CapabilityManager.ASK_EACH_TIME,
            issued_by="test",
        )

        with self.assertRaises(HumanApprovalRequired):
            self.runtime.tools.call(pid, "write_text_file", {"path": path, "content": "denied"})
        processed = self.runtime.human.drain_terminal_queue(auto_approve=False)

        self.assertEqual(processed[0].status, HumanRequestStatus.REJECTED)
        self.assertEqual(self.runtime.process.get(pid).status, ProcessStatus.RUNNABLE)
        self.assertFalse((self.runtime.workspace_root / path).exists())

    def test_per_use_prompt_describes_overwrite_risk(self) -> None:
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="review overwrite")
        path = self._path()
        target = self.runtime.workspace_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old content", encoding="utf-8")
        resource = self.runtime.filesystem.resource_for(path)
        self.runtime.capability.set_permission_policy(
            subject=pid,
            resource=resource,
            rights=[CapabilityRight.WRITE],
            policy=CapabilityManager.ASK_EACH_TIME,
            issued_by="test",
        )

        with self.assertRaises(HumanApprovalRequired):
            self.runtime.tools.call(pid, "write_text_file", {"path": path, "content": "new content"})
        request = self.runtime.human.pending()[0]
        context = request.payload["context"]

        self.assertTrue(context["will_overwrite"])
        self.assertFalse(context["will_create"])
        self.assertTrue(context["target"]["exists"])
        self.assertEqual(context["target"]["kind"], "file")
        self.assertEqual(context["target"]["size_bytes"], len("old content".encode("utf-8")))

    def test_llm_pending_per_use_approval_does_not_return_action_until_decision(self) -> None:
        path = self._path()
        client = FakeActionClient(
            [
                {
                    "action": "write_text_file",
                    "path": path,
                    "content": "approved after waiting",
                }
            ]
        )
        self.runtime.llm.client = client
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="write with per-use approval")
        self.runtime.capability.set_permission_policy(
            subject=pid,
            resource=self.runtime.filesystem.resource_for(path),
            rights=[CapabilityRight.WRITE],
            policy=CapabilityManager.ASK_EACH_TIME,
            issued_by="test",
        )

        waiting = self.runtime.run_next_process_once()
        self.assertTrue(waiting["waiting_human"])
        self.assertNotIn("action", waiting)
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.runtime.process.get(pid).status, ProcessStatus.WAITING_HUMAN)
        self.assertNotIn("tool_failed", self._event_types(pid))

        self.runtime.human.drain_terminal_queue(auto_approve=True)
        resumed = self.runtime.run_next_process_once()

        self.assertEqual(client.calls, 1)
        self.assertTrue(resumed["resumed_after_human"])
        self.assertEqual(resumed["action"]["action"], "write_text_file")
        self.assertTrue(resumed["result"]["ok"])
        self.assertEqual(
            (self.runtime.workspace_root / path).read_text(encoding="utf-8"),
            "approved after waiting",
        )

    def _path(self) -> str:
        return f"agent_outputs/permission_policy_{uuid4().hex}.txt"

    def _event_types(self, pid: str) -> list[str]:
        return [event.type.value for event in self.runtime.events.list(target=pid)]


class FakeActionClient:
    def __init__(self, actions: list[dict[str, object]]):
        self.actions = list(actions)
        self.calls = 0

    def complete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        self.calls += 1
        action = self.actions.pop(0)
        name = str(action["action"])
        args = {key: value for key, value in action.items() if key != "action"}
        return LLMCompletion(
            content="",
            tool_calls=[{"id": f"fake_{self.calls}", "name": name, "arguments": json.dumps(args)}],
        )


if __name__ == "__main__":
    unittest.main()

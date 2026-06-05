from __future__ import annotations

import unittest
from uuid import uuid4

from agent_libos import Runtime
from agent_libos.models import CapabilityRight, ForkMode, HumanRequestStatus


class ExternalBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Runtime.open("local")
        self.human_output: list[str] = []
        self.runtime.substrate.human.output_sink = self.human_output.append

    def tearDown(self) -> None:
        self.runtime.close()

    def test_read_file_tool_cannot_bypass_filesystem_capability(self) -> None:
        path = self._write_workspace_fixture("hello from workspace")
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="read a file")

        denied = self.runtime.tools.call(pid, "read_text_file", {"path": path})
        self.assertFalse(denied.ok)
        self.assertIn("lacks read", denied.error or "")
        self.assertNotIn("primitive.filesystem.read_text", self._audit_actions())

        self.runtime.filesystem.grant_path(pid, path, [CapabilityRight.READ], issued_by="test")
        allowed = self.runtime.tools.call(pid, "read_text_file", {"path": path})
        self.assertTrue(allowed.ok)
        self.assertEqual(allowed.payload["content"], "hello from workspace")
        self.assertIn("primitive.filesystem.read_text", self._audit_actions())

    def test_write_file_tool_cannot_bypass_filesystem_capability(self) -> None:
        path = f"agent_outputs/boundary_write_{uuid4().hex}.txt"
        target = self.runtime.workspace_root / path
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="write a file")

        denied = self.runtime.tools.call(pid, "write_text_file", {"path": path, "content": "denied"})
        self.assertFalse(denied.ok)
        self.assertFalse(target.exists())

        self.runtime.filesystem.grant_path(pid, path, [CapabilityRight.WRITE], issued_by="test")
        allowed = self.runtime.tools.call(pid, "write_text_file", {"path": path, "content": "allowed"})
        self.assertTrue(allowed.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "allowed")
        self.assertIn("primitive.filesystem.write_text", self._audit_actions())

    def test_write_precondition_does_not_leak_existing_file_without_capability(self) -> None:
        path = self._write_workspace_fixture("existing")
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="probe existing file")

        denied = self.runtime.tools.call(
            pid,
            "write_text_file",
            {"path": path, "content": "new", "overwrite": False},
        )

        self.assertFalse(denied.ok)
        self.assertIn("lacks write", denied.error or "")
        self.assertNotIn("already exists", denied.error or "")
        self.assertEqual((self.runtime.workspace_root / path).read_text(encoding="utf-8"), "existing")

    def test_delete_precondition_does_not_leak_missing_file_without_capability(self) -> None:
        path = f"agent_outputs/missing_delete_{uuid4().hex}.txt"
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="probe missing file")

        denied = self.runtime.tools.call(pid, "delete_file", {"path": path})

        self.assertFalse(denied.ok)
        self.assertIn("lacks delete", denied.error or "")
        self.assertNotIn("does not exist", denied.error or "")

    def test_human_output_tool_cannot_bypass_human_capability(self) -> None:
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="speak to the human")

        denied = self.runtime.tools.call(pid, "human_output", {"message": "denied"})
        self.assertFalse(denied.ok)
        self.assertEqual(self.human_output, [])

        self.runtime.capability.grant(pid, "human:owner", [CapabilityRight.WRITE], issued_by="test")
        allowed = self.runtime.tools.call(pid, "human_output", {"message": "allowed"})
        self.assertTrue(allowed.ok)
        self.assertEqual(self.human_output, ["allowed"])
        self.assertEqual(self.runtime.human.list(pid)[0].status, HumanRequestStatus.DELIVERED)
        self.assertIn("human.output", self._audit_actions())

    def test_process_cannot_call_tool_outside_creation_tool_table(self) -> None:
        pid = self.runtime.process.spawn(image="toolmaker-agent:v0", goal="call unavailable tool")

        denied = self.runtime.tools.call(pid, "write_text_file", {"path": "agent_outputs/no_tool.txt", "content": "x"})

        self.assertFalse(denied.ok)
        self.assertIn("not in process tool table", denied.error or "")
        self.assertNotIn("human.query", self._audit_actions())

    def test_path_escape_is_denied_by_filesystem_primitive(self) -> None:
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="escape workspace")
        self.runtime.filesystem.grant_workspace(pid, [CapabilityRight.WRITE], issued_by="test")

        denied = self.runtime.tools.call(pid, "write_text_file", {"path": "../outside.txt", "content": "denied"})

        self.assertFalse(denied.ok)
        self.assertIn("escapes filesystem adapter root", denied.error or "")
        self.assertNotIn("primitive.filesystem.write_text", self._audit_actions())

    def test_revoked_filesystem_capability_denies_write(self) -> None:
        path = f"agent_outputs/revoked_write_{uuid4().hex}.txt"
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="revoked write")
        cap = self.runtime.filesystem.grant_path(pid, path, [CapabilityRight.WRITE], issued_by="test")
        self.runtime.capability.revoke(cap.cap_id, revoked_by="test", reason="boundary test")

        denied = self.runtime.tools.call(pid, "write_text_file", {"path": path, "content": "denied"})

        self.assertFalse(denied.ok)
        self.assertFalse((self.runtime.workspace_root / path).exists())
        self.assertNotIn("primitive.filesystem.write_text", self._audit_actions())

    def test_fork_does_not_inherit_parent_filesystem_write_capability(self) -> None:
        path = f"agent_outputs/fork_write_{uuid4().hex}.txt"
        parent = self.runtime.process.spawn(image="review-agent:v0", goal="parent")
        self.runtime.filesystem.grant_path(parent, path, [CapabilityRight.WRITE], issued_by="test")
        child = self.runtime.process.fork(parent, goal="child", mode=ForkMode.WORKER)

        denied = self.runtime.tools.call(child, "write_text_file", {"path": path, "content": "denied"})
        allowed = self.runtime.tools.call(parent, "write_text_file", {"path": path, "content": "allowed"})

        self.assertFalse(denied.ok)
        self.assertTrue(allowed.ok)
        self.assertEqual((self.runtime.workspace_root / path).read_text(encoding="utf-8"), "allowed")

    def _write_workspace_fixture(self, content: str) -> str:
        path = f"agent_outputs/boundary_read_{uuid4().hex}.txt"
        target = self.runtime.workspace_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return path

    def _audit_actions(self) -> list[str]:
        return [record.action for record in self.runtime.audit.trace()]


if __name__ == "__main__":
    unittest.main()

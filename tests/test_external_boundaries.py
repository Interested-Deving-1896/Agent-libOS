from __future__ import annotations

import unittest
from uuid import uuid4

from agent_libos import Runtime
from agent_libos.models import CapabilityRight


class ExternalBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Runtime.open("local")
        self.human_output: list[str] = []
        self.runtime.human.output_sink = self.human_output.append

    def tearDown(self) -> None:
        self.runtime.close()

    def test_read_file_tool_cannot_bypass_filesystem_capability(self) -> None:
        path = self._write_workspace_fixture("hello from workspace")
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="read a file")
        self.runtime.tools.grant_execute(pid, "read_text_file", issued_by="test")

        denied = self.runtime.tools.call(pid, "read_text_file", {"path": path})
        self.assertFalse(denied.ok)
        self.assertIn("lacks read", denied.error or "")
        self.assertNotIn("external.filesystem.read_text", self._audit_actions())

        self.runtime.filesystem.grant_path(pid, path, [CapabilityRight.READ], issued_by="test")
        allowed = self.runtime.tools.call(pid, "read_text_file", {"path": path})
        self.assertTrue(allowed.ok)
        self.assertEqual(allowed.payload["content"], "hello from workspace")
        self.assertIn("external.filesystem.read_text", self._audit_actions())

    def test_write_file_tool_cannot_bypass_filesystem_capability(self) -> None:
        path = f"agent_outputs/boundary_write_{uuid4().hex}.txt"
        target = self.runtime.workspace_root / path
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="write a file")
        self.runtime.tools.grant_execute(pid, "write_text_file", issued_by="test")

        denied = self.runtime.tools.call(pid, "write_text_file", {"path": path, "content": "denied"})
        self.assertFalse(denied.ok)
        self.assertFalse(target.exists())

        self.runtime.filesystem.grant_path(pid, path, [CapabilityRight.WRITE], issued_by="test")
        allowed = self.runtime.tools.call(pid, "write_text_file", {"path": path, "content": "allowed"})
        self.assertTrue(allowed.ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "allowed")
        self.assertIn("external.filesystem.write_text", self._audit_actions())

    def test_human_output_tool_cannot_bypass_human_capability(self) -> None:
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="speak to the human")
        self.runtime.tools.grant_execute(pid, "human_output", issued_by="test")

        denied = self.runtime.tools.call(pid, "human_output", {"message": "denied"})
        self.assertFalse(denied.ok)
        self.assertEqual(self.human_output, [])

        self.runtime.capability.grant(pid, "human:owner", [CapabilityRight.WRITE], issued_by="test")
        allowed = self.runtime.tools.call(pid, "human_output", {"message": "allowed"})
        self.assertTrue(allowed.ok)
        self.assertEqual(self.human_output, ["allowed"])
        self.assertIn("human.output", self._audit_actions())

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

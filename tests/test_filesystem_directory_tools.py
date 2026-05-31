from __future__ import annotations

import unittest
from uuid import uuid4

from agent_libos import Runtime
from agent_libos.capability.manager import CapabilityManager
from agent_libos.exceptions import HumanApprovalRequired
from agent_libos.models import CapabilityRight, HumanRequestStatus


class FilesystemDirectoryToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Runtime.open("local")
        self.human_output: list[str] = []
        self.runtime.human.output_sink = self.human_output.append

    def tearDown(self) -> None:
        self.runtime.close()

    def test_read_write_and_delete_directory_and_file_tools(self) -> None:
        base = f"agent_outputs/fs_ops_{uuid4().hex}"
        existing_file = self._write_fixture(f"{base}/existing.txt", "existing")
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="filesystem ops")
        self.runtime.filesystem.grant_path_list(
            pid,
            read_dirs=[base],
            write_dirs=[base],
            delete_dirs=[base],
            issued_by="test",
        )

        listed = self.runtime.tools.call(pid, "read_directory", {"path": base})
        made_dir = self.runtime.tools.call(pid, "write_directory", {"path": f"{base}/created/nested"})
        wrote_file = self.runtime.tools.call(
            pid,
            "write_text_file",
            {"path": f"{base}/created/nested/out.txt", "content": "created"},
        )
        deleted_file = self.runtime.tools.call(pid, "delete_file", {"path": existing_file})
        deleted_dir = self.runtime.tools.call(
            pid,
            "delete_directory",
            {"path": f"{base}/created", "recursive": True},
        )

        self.assertTrue(listed.ok, listed.error)
        self.assertEqual([entry["name"] for entry in listed.payload["entries"]], ["existing.txt"])
        self.assertTrue(made_dir.ok, made_dir.error)
        self.assertTrue(made_dir.payload["created"])
        self.assertTrue(wrote_file.ok, wrote_file.error)
        self.assertTrue(deleted_file.ok, deleted_file.error)
        self.assertFalse((self.runtime.workspace_root / existing_file).exists())
        self.assertTrue(deleted_dir.ok, deleted_dir.error)
        self.assertFalse((self.runtime.workspace_root / base / "created").exists())

    def test_delete_requires_delete_capability_not_write_capability(self) -> None:
        path = self._write_fixture(f"agent_outputs/delete_denied_{uuid4().hex}.txt", "keep")
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="delete denied")
        self.runtime.filesystem.grant_path(pid, path, [CapabilityRight.WRITE], issued_by="test")

        denied = self.runtime.tools.call(pid, "delete_file", {"path": path})

        self.assertFalse(denied.ok)
        self.assertIn("lacks delete", denied.error or "")
        self.assertTrue((self.runtime.workspace_root / path).exists())

    def test_delete_ask_each_time_uses_filesystem_primitive_context(self) -> None:
        path = self._write_fixture(f"agent_outputs/delete_prompt_{uuid4().hex}.txt", "delete me")
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="delete with prompt")
        resource = self.runtime.filesystem.resource_for_path(path)
        self.runtime.capability.set_permission_policy(
            subject=pid,
            resource=resource,
            rights=[CapabilityRight.DELETE],
            policy=CapabilityManager.ASK_EACH_TIME,
            issued_by="test",
        )

        with self.assertRaises(HumanApprovalRequired):
            self.runtime.tools.call(pid, "delete_file", {"path": path})
        request = self.runtime.human.pending()[0]
        processed = self.runtime.human.drain_terminal_queue(auto_approve=True)
        retried = self.runtime.tools.call(pid, "delete_file", {"path": path})

        self.assertEqual(request.payload["context"]["primitive"], "runtime.filesystem.delete_file")
        self.assertEqual(request.payload["context"]["operation"], "delete_file")
        self.assertEqual(request.payload["context"]["right"], "delete")
        self.assertIn("target", request.payload["context"])
        self.assertEqual(processed[0].status, HumanRequestStatus.APPROVED)
        self.assertTrue(retried.ok, retried.error)
        self.assertFalse((self.runtime.workspace_root / path).exists())

    def _write_fixture(self, path: str, content: str) -> str:
        target = self.runtime.workspace_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()

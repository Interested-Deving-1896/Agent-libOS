from __future__ import annotations

import unittest
from uuid import uuid4

from agent_libos import Runtime
from agent_libos.api.cli import DEMO_PATCH_PREVIEW_CONTENT, DEMO_PATCH_PREVIEW_PATH, run_demo
from agent_libos.exceptions import HumanApprovalRequired
from agent_libos.models import HumanRequestStatus, ProcessStatus


class DemoContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Runtime.open("local")

    def tearDown(self) -> None:
        self.runtime.close()

    def test_run_demo_returns_auditable_contract(self) -> None:
        result = run_demo(self.runtime)

        self.assertTrue(result["root"].startswith("pid_"))
        self.assertTrue(result["worker"].startswith("pid_"))
        self.assertTrue(result["checkpoint"].startswith("ckpt_"))
        self.assertTrue(result["final_report_oid"].startswith("obj_"))
        self.assertIsNotNone(result["approval_request"])
        self.assertGreater(result["audit_records"], 0)

        self.assertFalse(result["filesystem_write_denial"]["ok"])
        self.assertIn("lacks write", result["filesystem_write_denial"]["error"])
        self.assertTrue(result["write_result"]["ok"])
        self.assertEqual(result["write_result"]["payload"]["path"], DEMO_PATCH_PREVIEW_PATH)
        self.assertTrue(result["target_file_exists"])
        self.assertTrue(result["target_file_content_matches"])

        target = self.runtime.workspace_root / DEMO_PATCH_PREVIEW_PATH
        self.assertEqual(target.read_text(encoding="utf-8"), DEMO_PATCH_PREVIEW_CONTENT)

        tool_names = [entry["tool"] for entry in result["tool_sequence"]]
        self.assertIn("parse_pytest_log", tool_names)
        self.assertIn("extract_failed_tests", tool_names)
        self.assertGreaterEqual(tool_names.count("write_text_file"), 3)

        report = self.runtime.store.get_object(result["final_report_oid"])
        self.assertIsNotNone(report)
        assert report is not None
        payload = report.payload
        self.assertEqual(payload["problem"]["failed_test"], "tests/test_math.py::test_add")
        self.assertEqual(payload["authorization"]["tool_execute_approval_request"], result["approval_request"])
        self.assertFalse(payload["authorization"]["filesystem_write_denied_before_grant"]["ok"])
        self.assertEqual(payload["external_side_effects"][0]["path"], DEMO_PATCH_PREVIEW_PATH)
        self.assertTrue(payload["target_file"]["content_matches"])
        self.assertIn("not a production automatic repair system", payload["limits"])

        audit_actions = [record.action for record in self.runtime.audit.trace()]
        for action in [
            "checkpoint.create",
            "human.query",
            "human.response",
            "external.filesystem.write_text",
            "tool.call",
            "process.exit",
        ]:
            self.assertIn(action, audit_actions)

        event_types = [event.type.value for event in self.runtime.events.list()]
        self.assertIn("external_write", event_types)
        self.assertIn("human_query", event_types)
        self.assertIn("human_response", event_types)

    def test_missing_tool_execute_requests_human_approval(self) -> None:
        pid = self.runtime.process.spawn(image="review-agent:v0", goal="write a demo file")
        path = f"agent_outputs/demo_missing_tool_{uuid4().hex}.txt"

        with self.assertRaises(HumanApprovalRequired) as caught:
            self.runtime.tools.call(pid, "write_text_file", {"path": path, "content": "denied"})

        request = self.runtime.human.get(caught.exception.request_id)
        self.assertEqual(request.status, HumanRequestStatus.PENDING)
        self.assertEqual(request.pid, pid)
        self.assertEqual(request.payload["requested_capability"]["subject"], pid)
        self.assertEqual(request.payload["requested_capability"]["rights"], ["execute"])
        self.assertIn("tool:", request.payload["requested_capability"]["resource"])

        process = self.runtime.process.get(pid)
        self.assertEqual(process.status, ProcessStatus.WAITING_HUMAN)
        self.assertIn("waiting for human request", process.status_message or "")
        self.assertFalse((self.runtime.workspace_root / path).exists())
        self.assertIn("human.query", [record.action for record in self.runtime.audit.trace()])


if __name__ == "__main__":
    unittest.main()

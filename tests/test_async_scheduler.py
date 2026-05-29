from __future__ import annotations

import asyncio
import json
import unittest
from uuid import uuid4

from agent_libos import Runtime
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import CapabilityRight, HumanRequestStatus, ProcessStatus
from scripts.async_clock_interleave_smoke import run_interleaved_clock_demo


class AsyncSchedulerTests(unittest.TestCase):
    def test_two_processes_alternate_time_output_via_async_sleep(self) -> None:
        report = asyncio.run(
            run_interleaved_clock_demo(
                iterations=2,
                interval_s=0.04,
                offset_s=0.02,
                echo=False,
            )
        )

        self.assertTrue(report["interleaved"])
        self.assertEqual(report["actual_order"], ["A", "B", "A", "B"])
        self.assertTrue(all(status == "exited" for status in report["process_statuses"].values()))
        self.assertTrue(all("+08:00" in output["message"] for output in report["outputs"]))
        self.assertGreaterEqual(report["model_calls"], 10)

    def test_async_runtime_drains_human_queue_and_resumes_pending_permission_action(self) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.human.output_sink = lambda _message: None
            path = f"agent_outputs/async_permission_{uuid4().hex}.txt"
            resource = runtime.filesystem.resource_for(path)
            runtime.llm.client = PlannedActionClient(
                [
                    {"action": "write_text_file", "path": path, "content": "approved through async queue"},
                    {"action": "process_exit", "payload": {"written": True}},
                ]
            )
            pid = runtime.process.spawn(image="review-agent:v0", goal="write with per-use human approval")
            runtime.capability.set_permission_policy(
                subject=pid,
                resource=resource,
                rights=[CapabilityRight.WRITE],
                policy="ask_each_time",
                issued_by="test",
            )

            results = asyncio.run(
                runtime.arun_until_idle(
                    max_quanta=4,
                    human_auto_approve=True,
                )
            )

            self.assertEqual(runtime.process.get(pid).status, ProcessStatus.EXITED)
            self.assertEqual((runtime.workspace_root / path).read_text(encoding="utf-8"), "approved through async queue")
            self.assertEqual([_action_name(result) for result in results], [None, "write_text_file", "process_exit"])
            request = runtime.human.list(pid)[0]
            self.assertEqual(request.status, HumanRequestStatus.APPROVED)
        finally:
            runtime.close()


class PlannedActionClient:
    def __init__(self, actions: list[dict[str, object]]):
        self.actions = list(actions)

    def complete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        if not self.actions:
            raise AssertionError("no planned action remains")
        action = self.actions.pop(0)
        name = str(action["action"])
        args = {key: value for key, value in action.items() if key != "action"}
        return LLMCompletion(
            content="",
            tool_calls=[{"id": f"planned_{len(self.actions)}", "name": name, "arguments": json.dumps(args)}],
        )


def _action_name(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    action = result.get("action")
    if isinstance(action, dict):
        return action.get("action")
    return None


if __name__ == "__main__":
    unittest.main()

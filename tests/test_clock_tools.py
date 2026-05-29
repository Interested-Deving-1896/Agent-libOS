from __future__ import annotations

import time
import unittest
from datetime import datetime

from agent_libos import Runtime


class ClockToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Runtime.open("local")

    def tearDown(self) -> None:
        self.runtime.close()

    def test_current_time_tool_uses_clock_primitive(self) -> None:
        pid = self.runtime.process.spawn(image="base-agent:v0", goal="check time")

        result = self.runtime.tools.call(pid, "get_current_time", {"timezone": "UTC"})

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.payload["timezone"], "UTC")
        parsed = datetime.fromisoformat(result.payload["iso8601"])
        self.assertIsNotNone(parsed.tzinfo)
        self.assertIn("external.clock.now", self._audit_actions())
        self.assertTrue(
            any(event.target == "clock:now" and event.payload.get("operation") == "now" for event in self.runtime.events.list())
        )

    def test_current_time_tool_supports_iana_timezone(self) -> None:
        pid = self.runtime.process.spawn(image="base-agent:v0", goal="check shanghai time")

        result = self.runtime.tools.call(pid, "get_current_time", {"timezone": "Asia/Shanghai"})

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.payload["timezone"], "Asia/Shanghai")
        self.assertIn("+08:00", result.payload["iso8601"])

    def test_sleep_tool_uses_clock_primitive(self) -> None:
        pid = self.runtime.process.spawn(image="base-agent:v0", goal="sleep briefly")

        started = time.monotonic()
        result = self.runtime.tools.call(pid, "sleep", {"seconds": 0.02})
        elapsed = time.monotonic() - started

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.payload["requested_seconds"], 0.02)
        self.assertGreaterEqual(result.payload["elapsed_seconds"], 0.0)
        self.assertGreaterEqual(elapsed, 0.015)
        self.assertIn("external.clock.sleep", self._audit_actions())
        self.assertTrue(
            any(
                event.target == "clock:sleep" and event.payload.get("operation") == "sleep"
                for event in self.runtime.events.list()
            )
        )

    def test_sleep_tool_rejects_unbounded_duration(self) -> None:
        pid = self.runtime.process.spawn(image="base-agent:v0", goal="sleep too long")

        result = self.runtime.tools.call(pid, "sleep", {"seconds": 61})

        self.assertFalse(result.ok)
        self.assertIn("Invalid arguments", result.error or "")
        self.assertNotIn("external.clock.sleep", self._audit_actions())

    def test_clock_tools_are_in_process_tool_table(self) -> None:
        pid = self.runtime.process.spawn(image="base-agent:v0", goal="time tools")
        names = {schema["function"]["name"] for schema in self.runtime.tools.openai_tool_schemas(pid)}

        self.assertIn("get_current_time", names)
        self.assertIn("sleep", names)

    def _audit_actions(self) -> list[str]:
        return [record.action for record in self.runtime.audit.trace()]


if __name__ == "__main__":
    unittest.main()

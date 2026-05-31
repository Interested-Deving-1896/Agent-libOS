from __future__ import annotations

import unittest

from agent_libos.llm.tool_protocol import tool_call_to_action


class ToolProtocolTests(unittest.TestCase):
    def test_tool_name_wins_over_action_argument(self) -> None:
        action = tool_call_to_action(
            {
                "name": "read_directory",
                "arguments": '{"action": "delete_directory", "path": "."}',
            }
        )

        self.assertEqual(action, {"action": "read_directory", "path": "."})

    def test_empty_tool_name_can_use_fallback_action_argument(self) -> None:
        action = tool_call_to_action(
            {
                "name": "",
                "arguments": '{"action": "read_directory", "path": "."}',
            }
        )

        self.assertEqual(action, {"action": "read_directory", "path": "."})

    def test_empty_tool_name_without_fallback_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            tool_call_to_action({"name": "", "arguments": '{"path": "."}'})


if __name__ == "__main__":
    unittest.main()

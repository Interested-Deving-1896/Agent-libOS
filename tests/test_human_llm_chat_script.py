from __future__ import annotations

import asyncio
import unittest

from scripts.human_llm_chat import EchoResponder, run_chat


class HumanLLMChatScriptTests(unittest.TestCase):
    def test_chat_uses_human_question_and_output_tools(self) -> None:
        report = asyncio.run(
            run_chat(
                responder=EchoResponder(),
                max_turns=5,
                auto_messages=["hello", "/exit"],
                echo=False,
            )
        )

        self.assertEqual(report["process_status"], "exited")
        self.assertEqual(report["turns"], 1)
        self.assertEqual(report["history"], [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "Echo: hello"}])
        self.assertIn("Assistant: Echo: hello", report["outputs"])
        self.assertIn("Assistant: goodbye.", report["outputs"])
        self.assertEqual(
            report["actions"],
            [None, "ask_human", "human_output", None, "ask_human", "human_output", "process_exit"],
        )


if __name__ == "__main__":
    unittest.main()

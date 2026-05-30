from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from uuid import uuid4

from scripts.ask_file_then_show import run_file_viewer


class AskFileThenShowScriptTests(unittest.TestCase):
    def test_script_asks_for_file_and_outputs_content(self) -> None:
        relative = f"agent_outputs/ask_file_then_show_{uuid4().hex}.txt"
        target = Path(relative)
        content = "human selected this file\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        report = asyncio.run(
            run_file_viewer(
                auto_answer=relative,
                max_bytes=1024,
                max_quanta=6,
                echo=False,
            )
        )

        self.assertEqual(report["process_status"], "exited")
        self.assertEqual(report["selected_path"], relative)
        self.assertTrue(report["displayed"])
        self.assertIsNone(report["error"])
        self.assertIn(content.strip(), report["outputs"][-1])
        self.assertEqual(
            report["actions"],
            [None, "ask_human", "read_text_file", "human_output", "process_exit"],
        )


if __name__ == "__main__":
    unittest.main()

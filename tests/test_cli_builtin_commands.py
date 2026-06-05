from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_libos import Runtime
from agent_libos.api.cli import main as cli_main
from agent_libos.models import ProcessMessageKind, ProcessStatus
from agent_libos.substrate import LocalResourceProviderSubstrate


class CLIBuiltinCommandTests(unittest.TestCase):
    def test_cli_cd_changes_process_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "pkg").mkdir()
            db = root / "runtime.sqlite"
            with _temporary_cwd(root):
                spawn = _run_cli_json(["--db", str(db), "spawn", "--image", "review-agent:v0", "--goal", "set cwd"])
                result = _run_cli_json(["--db", str(db), "cd", spawn["pid"], "pkg"])

            runtime = Runtime.open(db, substrate=LocalResourceProviderSubstrate(root))
            try:
                self.assertEqual(result["pid"], spawn["pid"])
                self.assertEqual(result["working_directory"], "pkg")
                self.assertEqual(runtime.process.get(spawn["pid"]).working_directory, "pkg")
            finally:
                runtime.close()

    def test_cli_exit_marks_process_exited_with_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "runtime.sqlite"
            with _temporary_cwd(root):
                spawn = _run_cli_json(["--db", str(db), "spawn", "--image", "base-agent:v0", "--goal", "finish"])
                result = _run_cli_json(["--db", str(db), "exit", spawn["pid"], "--payload", '{"done": true}'])

            runtime = Runtime.open(db, substrate=LocalResourceProviderSubstrate(root))
            try:
                process = runtime.process.get(spawn["pid"])
                self.assertEqual(result["pid"], spawn["pid"])
                self.assertEqual(result["status"], ProcessStatus.EXITED.value)
                self.assertIsNotNone(result["result_oid"])
                self.assertEqual(process.status, ProcessStatus.EXITED)
                self.assertTrue((process.status_message or "").startswith("result_oid:"))
            finally:
                runtime.close()

    def test_cli_exec_loads_yaml_image_from_first_arg_and_uses_second_arg_as_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "runtime.sqlite"
            manifest = root / "image.yaml"
            manifest.write_text(
                """
image:
  image_id: cli-yaml-agent:v0
  name: cli-yaml-agent
  system_prompt: |
    CLI loaded image.
  default_tools:
    - human_output
  context_policy: evidence_first
""".lstrip(),
                encoding="utf-8",
            )
            with _temporary_cwd(root):
                spawn = _run_cli_json(["--db", str(db), "spawn", "--image", "base-agent:v0", "--goal", "old goal"])
                before = Runtime.open(db, substrate=LocalResourceProviderSubstrate(root))
                try:
                    old_goal_oid = before.process.get(spawn["pid"]).goal_oid
                finally:
                    before.close()
                result = _run_cli_json(
                    [
                        "--db",
                        str(db),
                        "exec",
                        str(manifest),
                        "new goal from first arg",
                        "--pid",
                        spawn["pid"],
                        "--no-run",
                    ]
                )

            runtime = Runtime.open(db, substrate=LocalResourceProviderSubstrate(root))
            try:
                process = runtime.process.get(spawn["pid"])
                self.assertEqual(result["goal"], "new goal from first arg")
                self.assertEqual(result["image_arg"], str(manifest))
                self.assertEqual(result["loaded_image"]["image_id"], "cli-yaml-agent:v0")
                self.assertEqual(result["process"]["image"], "cli-yaml-agent:v0")
                self.assertFalse(result["ran"])
                self.assertEqual(process.image_id, "cli-yaml-agent:v0")
                self.assertNotEqual(process.goal_oid, old_goal_oid)
                self.assertIn("human_output", process.tool_table)
            finally:
                runtime.close()

    def test_cli_message_and_interrupt_post_human_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "runtime.sqlite"
            with _temporary_cwd(root):
                spawn = _run_cli_json(["--db", str(db), "spawn", "--image", "base-agent:v0", "--goal", "listen"])
                normal = _run_cli_json(
                    [
                        "--db",
                        str(db),
                        "message",
                        spawn["pid"],
                        "please inspect the latest result",
                        "--subject",
                        "status",
                    ]
                )
                interrupt = _run_cli_json(
                    [
                        "--db",
                        str(db),
                        "interrupt",
                        spawn["pid"],
                        "stop and read this first",
                    ]
                )

            runtime = Runtime.open(db, substrate=LocalResourceProviderSubstrate(root))
            try:
                unread = runtime.messages.unread(spawn["pid"])

                self.assertEqual(normal["message"]["kind"], ProcessMessageKind.NORMAL.value)
                self.assertEqual(interrupt["message"]["kind"], ProcessMessageKind.INTERRUPT.value)
                self.assertEqual([message.message_id for message in unread], [normal["message"]["message_id"], interrupt["message"]["message_id"]])
                self.assertEqual(unread[0].sender, "human:owner")
                self.assertEqual(unread[0].subject, "status")
                self.assertEqual(unread[1].subject, "Human interrupt")
            finally:
                runtime.close()


@contextlib.contextmanager
def _temporary_cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _run_cli_json(argv: list[str]) -> dict[str, object]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        cli_main(argv)
    return json.loads(stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

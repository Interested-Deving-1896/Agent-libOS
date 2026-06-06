from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_libos import Runtime
from agent_libos.models import (
    CapabilityRight,
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
)
from agent_libos.substrate import CommandResult, LocalResourceProviderSubstrate


class RecordingShellProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, argv: list[str], *, timeout: float = 30.0, cwd: str | None = None) -> CommandResult:
        self.calls.append((list(argv), cwd))
        return CommandResult(argv=list(argv), returncode=0, stdout="ok", stderr="")

    def classify_external_effect(self, operation: str, context: dict, result: object) -> ExternalEffectClassification:
        return ExternalEffectClassification(
            rollback_class=ExternalEffectRollbackClass.IRREVERSIBLE,
            rollback_status=ExternalEffectRollbackStatus.NOT_SUPPORTED,
            state_mutation=True,
            information_flow=True,
            metadata={"operation": operation},
        )


class ProcessWorkingDirectoryTests(unittest.TestCase):
    def test_filesystem_tools_resolve_paths_from_process_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "pkg").mkdir()
            (root / "pkg" / "module.py").write_text("print('pkg')\n", encoding="utf-8")
            runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(root))
            try:
                pid = runtime.process.spawn(image="review-agent:v0", goal="read from cwd")
                runtime.filesystem.grant_directory(pid, "pkg", [CapabilityRight.READ, CapabilityRight.WRITE], issued_by="test")

                changed = runtime.tools.call(pid, "set_working_directory", {"path": "pkg"})
                read = runtime.tools.call(pid, "read_text_file", {"path": "module.py"})
                written = runtime.tools.call(pid, "write_text_file", {"path": "created.txt", "content": "ok"})

                self.assertTrue(changed.ok, changed.error)
                self.assertEqual(changed.payload["working_directory"], "pkg")
                self.assertTrue(read.ok, read.error)
                self.assertEqual(read.payload["path"], "pkg/module.py")
                self.assertTrue(written.ok, written.error)
                self.assertTrue((root / "pkg" / "created.txt").exists())
            finally:
                runtime.close()

    def test_children_inherit_parent_working_directory_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "child-cwd").mkdir()
            runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(root))
            try:
                parent = runtime.process.spawn(image="review-agent:v0", goal="spawn child")
                self.assertTrue(runtime.tools.call(parent, "set_working_directory", {"path": "child-cwd"}).ok)

                spawned = runtime.tools.call(parent, "spawn_child_process", {"goal": "inherit cwd"})
                forked = runtime.tools.call(parent, "fork_child_process", {"goal": "inherit cwd"})

                self.assertTrue(spawned.ok, spawned.error)
                self.assertTrue(forked.ok, forked.error)
                self.assertEqual(runtime.process.get(spawned.payload["child_pid"]).working_directory, "child-cwd")
                self.assertEqual(runtime.process.get(forked.payload["child_pid"]).working_directory, "child-cwd")
            finally:
                runtime.close()

    def test_process_working_directory_persists_in_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "persisted").mkdir()
            db_path = root / "runtime.sqlite"
            runtime = Runtime.open(db_path, substrate=LocalResourceProviderSubstrate(root))
            try:
                pid = runtime.process.spawn(image="review-agent:v0", goal="persist cwd")
                self.assertTrue(runtime.tools.call(pid, "set_working_directory", {"path": "persisted"}).ok)
            finally:
                runtime.close()

            reopened = Runtime.open(db_path, substrate=LocalResourceProviderSubstrate(root))
            try:
                self.assertEqual(reopened.process.get(pid).working_directory, "persisted")
            finally:
                reopened.close()

    def test_shell_tool_runs_from_process_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "commands").mkdir()
            shell = RecordingShellProvider()
            substrate = LocalResourceProviderSubstrate(root)
            substrate.shell = shell
            runtime = Runtime.open("local", substrate=substrate)
            try:
                pid = runtime.process.spawn(image="review-agent:v0", goal="run from cwd")
                runtime.shell.grant_policy(pid, "always_allow", issued_by="test")

                self.assertTrue(runtime.tools.call(pid, "set_working_directory", {"path": "commands"}).ok)
                result = runtime.tools.call(pid, "run_shell_command", {"argv": ["echo", "hello"]})

                self.assertTrue(result.ok, result.error)
                self.assertEqual(shell.calls, [(["echo", "hello"], "commands")])
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()

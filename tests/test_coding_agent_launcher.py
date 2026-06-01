from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_libos import Runtime
from agent_libos.capability.manager import CapabilityManager
from agent_libos.models import CapabilityRight
from agent_libos.substrate import LocalResourceProviderSubstrate
from scripts import run_coding_agent


class CodingAgentLauncherTests(unittest.TestCase):
    def test_default_edit_preset_pregrants_workspace_write_not_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = run_coding_agent.build_parser().parse_args(
                ["--goal", "edit the workspace", "--workspace", tmp, "--no-run"]
            )
            runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(Path(tmp)))
            try:
                pid = runtime.process.spawn(image="coding-agent:v0", goal=args.goal)

                grants = run_coding_agent.configure_coding_agent_permissions(runtime, pid, args)

                self.assertTrue(grants)
                self.assertEqual(
                    runtime.capability.permission_policy(
                        pid,
                        runtime.filesystem.workspace_resource(),
                        CapabilityRight.WRITE,
                    ),
                    CapabilityManager.ALWAYS_ALLOW,
                )
                self.assertEqual(
                    runtime.capability.permission_policy(
                        pid,
                        runtime.filesystem.workspace_resource(),
                        CapabilityRight.DELETE,
                    ),
                    CapabilityManager.MISSING,
                )
            finally:
                runtime.close()

    def test_read_only_preset_can_add_specific_write_and_delete_grants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = run_coding_agent.build_parser().parse_args(
                [
                    "--goal",
                    "edit selected paths",
                    "--workspace",
                    tmp,
                    "--permission-preset",
                    "read-only",
                    "--write-file",
                    "src/main.py",
                    "--delete-dir",
                    "build",
                    "--no-run",
                ]
            )
            runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(Path(tmp)))
            try:
                pid = runtime.process.spawn(image="coding-agent:v0", goal=args.goal)

                run_coding_agent.configure_coding_agent_permissions(runtime, pid, args)

                self.assertEqual(
                    runtime.capability.permission_policy(
                        pid,
                        runtime.filesystem.resource_for_path("src/main.py"),
                        CapabilityRight.WRITE,
                    ),
                    CapabilityManager.ALWAYS_ALLOW,
                )
                self.assertEqual(
                    runtime.capability.permission_policy(
                        pid,
                        runtime.filesystem.directory_resource_for_path("build"),
                        CapabilityRight.DELETE,
                    ),
                    CapabilityManager.ALWAYS_ALLOW,
                )
                self.assertEqual(
                    runtime.capability.permission_policy(
                        pid,
                        runtime.filesystem.workspace_resource(),
                        CapabilityRight.WRITE,
                    ),
                    CapabilityManager.MISSING,
                )
            finally:
                runtime.close()

    def test_launcher_loads_project_env_before_workspace_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            env_file = project_root / ".env"
            env_file.write_text("OPENAI_MODEL=test\n", encoding="utf-8")
            args = run_coding_agent.build_parser().parse_args(
                ["--goal", "inspect", "--workspace", ".", "--no-run"]
            )

            with (
                patch.object(run_coding_agent, "PROJECT_ROOT", project_root),
                patch.object(run_coding_agent, "load_dotenv") as load,
            ):
                run_coding_agent._load_env(args)

        load.assert_called_once_with(env_file)

    def test_launcher_does_not_change_host_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            before = Path.cwd()
            args = run_coding_agent.build_parser().parse_args(
                ["--goal", "inspect", "--workspace", tmp, "--ephemeral-db", "--no-run"]
            )

            with patch.object(run_coding_agent, "load_dotenv"):
                asyncio.run(run_coding_agent.amain(args))

            self.assertEqual(Path.cwd(), before)

    def test_explicit_missing_env_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.env"
            args = run_coding_agent.build_parser().parse_args(
                ["--goal", "inspect", "--workspace", ".", "--env-file", str(missing), "--no-run"]
            )

            with self.assertRaises(SystemExit):
                run_coding_agent._load_env(args)

    def test_audit_counts_are_scoped_to_launched_process(self) -> None:
        records = [
            SimpleNamespace(actor="pid_current", action="llm.request"),
            SimpleNamespace(actor="pid_other", action="llm.action_repair_requested"),
            SimpleNamespace(actor="pid_current", action="llm.action_repair_requested"),
        ]

        counts = run_coding_agent._audit_counts_for_process(records, "pid_current")

        self.assertEqual(counts["audit_records"], 2)
        self.assertEqual(counts["audit_records_total"], 3)
        self.assertEqual(counts["llm_repair_attempts"], 1)


if __name__ == "__main__":
    unittest.main()

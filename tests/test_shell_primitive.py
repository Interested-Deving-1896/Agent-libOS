from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_libos import Runtime
from agent_libos.config import AgentLibOSConfig, ShellCommandRule, ShellDefaults
from agent_libos.models import CapabilityRight, HumanRequestStatus
from agent_libos.models.exceptions import CapabilityDenied, HumanApprovalRequired, ValidationError
from agent_libos.substrate import (
    CommandResult,
    LocalClockProvider,
    LocalFilesystemProvider,
    LocalHumanProvider,
    LocalResourceProviderSubstrate,
)


class ShellPrimitiveTests(unittest.TestCase):
    def test_whitelist_policy_auto_allows_exact_safe_command(self) -> None:
        runtime, provider = self._runtime_with_fake_shell()
        try:
            pid = runtime.process.spawn(image="review-agent:v0", goal="run safe shell")
            runtime.shell.grant_policy(pid, runtime.config.shell.allowlist_auto_else_ask_level, issued_by="test")

            result = runtime.shell.run(pid, ["git", "status", "--short"], timeout=2.0)

            self.assertEqual(result.stdout, "ok\n")
            self.assertEqual(provider.calls, [(["git", "status", "--short"], 2.0)])
            self.assertIn("primitive.shell.run", self._audit_actions(runtime))
        finally:
            runtime.close()

    def test_unlisted_command_requires_approval_and_consumes_once(self) -> None:
        runtime, provider = self._runtime_with_fake_shell()
        try:
            pid = runtime.process.spawn(image="review-agent:v0", goal="run unlisted shell")
            runtime.shell.grant_policy(pid, runtime.config.shell.allowlist_auto_else_ask_level, issued_by="test")

            with self.assertRaises(HumanApprovalRequired):
                runtime.shell.run(pid, ["git", "show", "--stat"])

            pending = runtime.human.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].payload["context"]["argv"], ["git", "show", "--stat"])
            self.assertEqual(runtime.human.drain_terminal_queue(auto_approve=True)[0].status, HumanRequestStatus.APPROVED)

            allowed = runtime.shell.run(pid, ["git", "show", "--stat"])
            self.assertEqual(allowed.stdout, "ok\n")
            self.assertEqual(provider.calls, [(["git", "show", "--stat"], runtime.config.tools.shell_timeout_s)])

            with self.assertRaises(HumanApprovalRequired):
                runtime.shell.run(pid, ["git", "show", "--stat"])
        finally:
            runtime.close()

    def test_always_deny_shell_policy_overrides_exact_command_grant(self) -> None:
        runtime, _provider = self._runtime_with_fake_shell()
        try:
            pid = runtime.process.spawn(image="review-agent:v0", goal="deny shell")
            runtime.shell.grant_policy(pid, runtime.config.shell.always_deny_level, issued_by="test")
            runtime.capability.grant(pid, "shell:git", [CapabilityRight.EXECUTE], issued_by="test")

            with self.assertRaises(CapabilityDenied):
                runtime.shell.run(pid, ["git", "status", "--short"])
        finally:
            runtime.close()

    def test_blacklist_policy_asks_for_nested_shell_interpreter(self) -> None:
        runtime, _provider = self._runtime_with_fake_shell()
        try:
            pid = runtime.process.spawn(image="review-agent:v0", goal="blacklist shell")
            runtime.shell.grant_policy(pid, runtime.config.shell.blocklist_ask_else_auto_level, issued_by="test")

            with self.assertRaises(HumanApprovalRequired):
                runtime.shell.run(pid, ["env", "bash", "-c", "echo unsafe"])

            request = runtime.human.pending()[0]
            self.assertEqual(request.payload["context"]["matched_rule"], ["bash"])
        finally:
            runtime.close()

    def test_path_qualified_whitelist_command_does_not_match_bare_rule(self) -> None:
        runtime, _provider = self._runtime_with_fake_shell()
        try:
            pid = runtime.process.spawn(image="review-agent:v0", goal="path bypass")
            runtime.shell.grant_policy(pid, runtime.config.shell.allowlist_auto_else_ask_level, issued_by="test")

            with self.assertRaises(HumanApprovalRequired):
                runtime.shell.run(pid, ["./git", "status", "--short"])
        finally:
            runtime.close()

    def test_shell_tool_uses_runtime_shell_primitive(self) -> None:
        runtime, provider = self._runtime_with_fake_shell()
        try:
            pid = runtime.process.spawn(image="review-agent:v0", goal="run shell tool")
            runtime.shell.grant_policy(pid, runtime.config.shell.allowlist_auto_else_ask_level, issued_by="test")

            result = runtime.tools.call(pid, "run_shell_command", {"argv": ["git", "status", "--short"]})

            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.payload["stdout"], "ok\n")
            self.assertEqual(provider.calls, [(["git", "status", "--short"], runtime.config.tools.shell_timeout_s)])
        finally:
            runtime.close()

    def test_shell_primitive_truncates_output_before_tool_layer(self) -> None:
        config = AgentLibOSConfig(
            shell=ShellDefaults(
                max_stdout_chars=3,
                max_stderr_chars=2,
                whitelist=(ShellCommandRule(("tool",)),),
                blacklist=(),
            )
        )
        runtime, provider = self._runtime_with_config(config)
        provider.stdout = "abcdef"
        provider.stderr = "wxyz"
        try:
            pid = runtime.process.spawn(image="review-agent:v0", goal="bounded shell")
            runtime.shell.grant_policy(pid, config.shell.allowlist_auto_else_ask_level, issued_by="test")

            result = runtime.shell.run(pid, ["tool"])
            tool_result = runtime.tools.call(
                pid,
                "run_shell_command",
                {"argv": ["tool"], "max_stdout_chars": 10, "max_stderr_chars": 10},
            )

            self.assertEqual(result.stdout, "abc")
            self.assertEqual(result.stderr, "wx")
            self.assertTrue(result.stdout_truncated)
            self.assertTrue(result.stderr_truncated)
            self.assertEqual(tool_result.payload["stdout"], "abc")
            self.assertTrue(tool_result.payload["stdout_truncated"])
            self.assertEqual(tool_result.payload["stderr"], "wx")
            self.assertTrue(tool_result.payload["stderr_truncated"])
        finally:
            runtime.close()

    def test_shell_primitive_enforces_timeout_limit_without_tool_schema(self) -> None:
        runtime, _provider = self._runtime_with_fake_shell()
        try:
            pid = runtime.process.spawn(image="review-agent:v0", goal="bounded timeout")
            runtime.shell.grant_policy(pid, runtime.config.shell.allowlist_auto_else_ask_level, issued_by="test")

            with self.assertRaises(ValidationError):
                runtime.shell.run(pid, ["git", "status", "--short"], timeout=0)
            with self.assertRaises(ValidationError):
                runtime.shell.run(
                    pid,
                    ["git", "status", "--short"],
                    timeout=runtime.config.shell.timeout_hard_limit_s + 1,
                )
        finally:
            runtime.close()

    def _runtime_with_fake_shell(self) -> tuple[Runtime, "FakeShellProvider"]:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        provider = FakeShellProvider()
        substrate = RecordingShellSubstrate(temp_dir.name, provider)
        runtime = Runtime.open("local", substrate=substrate)
        runtime.substrate.human.output_sink = lambda _message: None
        return runtime, provider

    def _runtime_with_config(self, config: AgentLibOSConfig) -> tuple[Runtime, "FakeShellProvider"]:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        provider = FakeShellProvider()
        substrate = RecordingShellSubstrate(temp_dir.name, provider)
        runtime = Runtime.open("local", substrate=substrate, config=config)
        runtime.substrate.human.output_sink = lambda _message: None
        return runtime, provider

    def _audit_actions(self, runtime: Runtime) -> list[str]:
        return [record.action for record in runtime.audit.trace()]


class ShellMatcherTests(unittest.TestCase):
    def test_custom_exact_whitelist_rule_does_not_prefix_match_extra_args(self) -> None:
        config = AgentLibOSConfig(
            shell=ShellDefaults(whitelist=(ShellCommandRule(("tool", "safe")),), blacklist=())
        )
        runtime, _provider = self._runtime_with_config(config)
        try:
            pid = runtime.process.spawn(image="review-agent:v0", goal="exact shell")
            runtime.shell.grant_policy(pid, config.shell.allowlist_auto_else_ask_level, issued_by="test")

            with self.assertRaises(HumanApprovalRequired):
                runtime.shell.run(pid, ["tool", "safe", "--extra"])
        finally:
            runtime.close()

    def _runtime_with_config(self, config: AgentLibOSConfig) -> tuple[Runtime, "FakeShellProvider"]:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        provider = FakeShellProvider()
        substrate = RecordingShellSubstrate(temp_dir.name, provider)
        runtime = Runtime.open("local", substrate=substrate, config=config)
        runtime.substrate.human.output_sink = lambda _message: None
        return runtime, provider


class RecordingShellSubstrate(LocalResourceProviderSubstrate):
    def __init__(self, root: str, shell: "FakeShellProvider"):
        self.workspace_root = Path(root).resolve()
        self.workspace_display = str(self.workspace_root)
        self.filesystem = LocalFilesystemProvider(root)
        self.clock = LocalClockProvider()
        self.shell = shell
        self.human = LocalHumanProvider()


class FakeShellProvider:
    def __init__(self):
        self.calls: list[tuple[list[str], float]] = []
        self.stdout = "ok\n"
        self.stderr = ""

    def run(self, argv: list[str], *, timeout: float = 30.0, cwd: str | None = None) -> CommandResult:
        self.calls.append((list(argv), timeout))
        return CommandResult(argv=list(argv), returncode=0, stdout=self.stdout, stderr=self.stderr)


if __name__ == "__main__":
    unittest.main()

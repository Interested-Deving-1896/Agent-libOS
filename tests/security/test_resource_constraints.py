from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent_libos import Runtime
from agent_libos.models import CapabilityRight, ResourceBudget
from agent_libos.models.exceptions import ResourceLimitExceeded
from agent_libos.substrate import LocalFilesystemProvider, LocalResourceProviderSubstrate, ResolvedPath
from tests.security.test_shell_primitive import FakeShellProvider, RecordingShellSubstrate


class TestResourceConstraints:
    def test_capability_grant_does_not_expand_tool_call_budget(self) -> None:
        provider = FakeShellProvider()
        runtime = Runtime.open(
            "local",
            substrate=RecordingShellSubstrate(".", provider),
        )
        try:
            pid = runtime.process.spawn(
                image="base-agent:v0",
                goal="budget cannot be bypassed",
                resource_budget=ResourceBudget(max_tool_calls=0),
            )
            runtime.tools.configure_process_tools(pid, ["run_shell_command"], assigned_by="test")
            runtime.shell.grant_policy(pid, "always_allow", issued_by="test")
            runtime.capability.grant(pid, "shell:git", [CapabilityRight.EXECUTE], issued_by="test")

            result = runtime.tools.call(pid, "run_shell_command", {"argv": ["git", "status"]})

            assert not result.ok
            assert "max_tool_calls" in (result.error or "")
            assert provider.calls == []
        finally:
            runtime.close()

    def test_exec_process_does_not_reset_resource_usage_or_budget(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(
                image="base-agent:v0",
                goal="exec resource",
                resource_budget=ResourceBudget(max_tool_calls=2),
            )
            runtime.tools.configure_process_tools(pid, ["get_working_directory"], assigned_by="test")
            assert runtime.tools.call(pid, "get_working_directory", {}).ok

            runtime.exec_process(pid, "base-agent:v0", goal="after exec")
            process = runtime.process.get(pid)

            assert process.resource_budget.max_tool_calls == 2
            assert process.resource_usage.tool_calls == 1
        finally:
            runtime.close()

    def test_always_allow_shell_policy_does_not_bypass_subprocess_wall_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(
                    image="base-agent:v0",
                    goal="subprocess limit",
                    resource_budget=ResourceBudget(max_subprocess_wall_seconds=0.05),
                )
                runtime.tools.configure_process_tools(pid, ["run_shell_command"], assigned_by="test")
                runtime.shell.grant_policy(pid, "always_allow", issued_by="test")

                result = runtime.tools.call(
                    pid,
                    "run_shell_command",
                    {"argv": ["python", "-c", "import time; time.sleep(0.2)"], "timeout_s": 5.0},
                )

                assert not result.ok
                assert runtime.process.get(pid).status.value == "killed"
                assert any(record.action == "resource.limit_exceeded" for record in runtime.audit.trace())
            finally:
                runtime.close()

    def test_shell_provider_without_limits_support_fails_closed_when_budgeted(self) -> None:
        provider = NoLimitsShellProvider()
        runtime = Runtime.open(
            "local",
            substrate=RecordingShellSubstrate(".", provider),
        )
        try:
            pid = runtime.process.spawn(
                image="base-agent:v0",
                goal="provider limits required",
                resource_budget=ResourceBudget(max_subprocess_wall_seconds=1.0),
            )
            runtime.tools.configure_process_tools(pid, ["run_shell_command"], assigned_by="test")
            runtime.shell.grant_policy(pid, "always_allow", issued_by="test")

            result = runtime.tools.call(pid, "run_shell_command", {"argv": ["git", "status"]})

            assert not result.ok
            assert "SubprocessLimits" in (result.error or "")
            assert provider.calls == []
        finally:
            runtime.close()

    def test_shell_timeout_charges_metrics_without_killing_when_budget_remains(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(
                    image="base-agent:v0",
                    goal="timeout charge",
                    resource_budget=ResourceBudget(max_subprocess_wall_seconds=10.0),
                )
                runtime.shell.grant_policy(pid, "always_allow", issued_by="test")

                try:
                    runtime.shell.run(
                        pid,
                        ["python", "-c", "import time; time.sleep(0.2)"],
                        timeout=0.05,
                    )
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("expected shell timeout")

                process = runtime.process.get(pid)
                assert process.status.value == "runnable"
                assert process.resource_usage.subprocess_wall_seconds > 0
            finally:
                runtime.close()

    def test_terminal_process_cannot_call_visible_tools_directly(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="terminal")
            runtime.tools.configure_process_tools(pid, ["get_working_directory"], assigned_by="test")
            runtime.process.exit(pid, failed=False, message="done")

            result = runtime.tools.call(pid, "get_working_directory", {})

            assert not result.ok
            assert "terminal process" in (result.error or "")
        finally:
            runtime.close()

    def test_filesystem_read_uses_provider_limited_read_and_charges_actual_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            provider = RecordingLimitedFilesystemProvider(temp_dir)
            substrate = LocalResourceProviderSubstrate(temp_dir)
            substrate.filesystem = provider
            Path(temp_dir, "large.txt").write_text("x" * 100, encoding="utf-8")
            runtime = Runtime.open("local", substrate=substrate)
            try:
                pid = runtime.process.spawn(
                    image="base-agent:v0",
                    goal="limited read",
                    resource_budget=ResourceBudget(max_external_read_bytes=10),
                )
                runtime.filesystem.grant_path(pid, "large.txt", [CapabilityRight.READ], issued_by="test")

                result = runtime.filesystem.read_bytes(pid, "large.txt", max_bytes=10)

                assert result.bytes_read == 10
                assert result.truncated
                assert provider.read_limits == [10]
                assert runtime.process.get(pid).resource_usage.external_read_bytes == 10
            finally:
                runtime.close()

    def test_directory_listing_metadata_is_charged_to_external_read_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "item.txt").write_text("content", encoding="utf-8")
            runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(
                    image="base-agent:v0",
                    goal="directory budget",
                    resource_budget=ResourceBudget(max_external_read_bytes=1),
                )
                runtime.filesystem.grant_directory(pid, ".", [CapabilityRight.READ], issued_by="test")

                with pytest.raises(ResourceLimitExceeded):
                    runtime.filesystem.read_directory(pid, ".")

                assert runtime.process.get(pid).status.value == "killed"
                assert runtime.process.get(pid).resource_usage.external_read_bytes > 1
            finally:
                runtime.close()


class NoLimitsShellProvider(FakeShellProvider):
    def run(
        self,
        argv: list[str],
        *,
        timeout: float = 30.0,
        cwd: str | None = None,
        stdout_limit_chars: int | None = None,
        stderr_limit_chars: int | None = None,
    ):
        self.calls.append((list(argv), timeout))
        return super().run(
            argv,
            timeout=timeout,
            cwd=cwd,
            stdout_limit_chars=stdout_limit_chars,
            stderr_limit_chars=stderr_limit_chars,
        )


class RecordingLimitedFilesystemProvider(LocalFilesystemProvider):
    def __init__(self, root: str):
        super().__init__(root)
        self.read_limits: list[int | None] = []

    def read_bytes(self, path: ResolvedPath, *, max_bytes: int | None = None) -> bytes:
        self.read_limits.append(max_bytes)
        return super().read_bytes(path, max_bytes=max_bytes)

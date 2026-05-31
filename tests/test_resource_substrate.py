from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from agent_libos import Runtime
from agent_libos.external.shell import ShellAdapter
from agent_libos.models import CapabilityRight
from agent_libos.substrate import (
    CommandResult,
    LocalClockProvider,
    LocalFilesystemProvider,
    LocalResourceProviderSubstrate,
    LocalShellProvider,
    ResolvedPath,
)


class ResourceProviderSubstrateTests(unittest.TestCase):
    def test_runtime_filesystem_primitive_uses_injected_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            substrate = RecordingSubstrate(temp_dir)
            runtime = Runtime.open("local", substrate=substrate)
            try:
                path = "agent_outputs/substrate_write.txt"
                pid = runtime.process.spawn(image="review-agent:v0", goal="write through substrate")
                runtime.filesystem.grant_path(pid, path, [CapabilityRight.WRITE], issued_by="test")

                result = runtime.tools.call(pid, "write_text_file", {"path": path, "content": "via provider"})

                self.assertTrue(result.ok, result.error)
                self.assertIn("resolve", substrate.filesystem.calls)
                self.assertIn("write_text", substrate.filesystem.calls)
                self.assertEqual((Path(temp_dir) / path).read_text(encoding="utf-8"), "via provider")
            finally:
                runtime.close()

    def test_runtime_clock_primitive_uses_injected_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_clock = FakeClockProvider()
            substrate = LocalResourceProviderSubstrate(temp_dir)
            substrate.clock = fake_clock
            runtime = Runtime.open("local", substrate=substrate)
            try:
                pid = runtime.process.spawn(image="base-agent:v0", goal="use fake clock")

                now = runtime.tools.call(pid, "get_current_time", {"timezone": "UTC"})
                slept = runtime.tools.call(pid, "sleep", {"seconds": 0.1})

                self.assertTrue(now.ok, now.error)
                self.assertEqual(now.payload["iso8601"], "2040-01-02T03:04:05+00:00")
                self.assertTrue(slept.ok, slept.error)
                self.assertEqual(slept.payload["elapsed_seconds"], 0.25)
                self.assertEqual(fake_clock.sleeps, [("async", 0.1)])
            finally:
                runtime.close()

    def test_shell_adapter_uses_injected_provider(self) -> None:
        runtime = Runtime.open("local")
        provider = FakeShellProvider()
        shell = ShellAdapter(runtime.capability, runtime.audit, provider=provider)
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="run shell through substrate")
            runtime.capability.grant(pid, "shell:git", [CapabilityRight.EXECUTE], issued_by="test")

            result = shell.run(pid, ["git", "status", "--short"], timeout=2.0)

            self.assertEqual(result.stdout, "ok\n")
            self.assertEqual(provider.calls, [(["git", "status", "--short"], 2.0)])
        finally:
            runtime.close()


class RecordingSubstrate:
    def __init__(self, root: str):
        self.workspace_root = Path(root).resolve()
        self.workspace_display = str(self.workspace_root)
        self.filesystem = RecordingFilesystemProvider(root)
        self.clock = LocalClockProvider()
        self.shell = LocalShellProvider(root)


class RecordingFilesystemProvider:
    def __init__(self, root: str):
        self.inner = LocalFilesystemProvider(root)
        self.namespace = self.inner.namespace
        self.root_display = self.inner.root_display
        self.calls: list[str] = []

    def resolve(self, path: Any) -> ResolvedPath:
        self.calls.append("resolve")
        return self.inner.resolve(path)

    def state(self, path: ResolvedPath):
        self.calls.append("state")
        return self.inner.state(path)

    def write_text(self, path: ResolvedPath, text: str, encoding: str, newline: str | None = "\n") -> None:
        self.calls.append("write_text")
        self.inner.write_text(path, text, encoding, newline)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)


class FakeClockProvider:
    def __init__(self):
        self.sleeps: list[tuple[str, float]] = []
        self._monotonic = [100.0, 100.25]

    def now(self, timezone_: tzinfo) -> datetime:
        return datetime(2040, 1, 2, 3, 4, 5, tzinfo=timezone_)

    def monotonic(self) -> float:
        return self._monotonic.pop(0)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(("sync", seconds))

    async def asleep(self, seconds: float) -> None:
        self.sleeps.append(("async", seconds))


class FakeShellProvider:
    def __init__(self):
        self.calls: list[tuple[list[str], float]] = []

    def run(self, argv: list[str], *, timeout: float = 30.0) -> CommandResult:
        self.calls.append((list(argv), timeout))
        return CommandResult(argv=list(argv), returncode=0, stdout="ok\n", stderr="")


if __name__ == "__main__":
    unittest.main()

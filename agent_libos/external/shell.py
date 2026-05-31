from __future__ import annotations

import os

from agent_libos.capability.manager import CapabilityManager
from agent_libos.models import CapabilityRight
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.substrate import CommandResult, LocalShellProvider, ShellProvider


class ShellAdapter:
    def __init__(
        self,
        capabilities: CapabilityManager,
        audit: AuditManager,
        cwd: str | os.PathLike[str] | None = None,
        provider: ShellProvider | None = None,
    ):
        self.capabilities = capabilities
        self.audit = audit
        self.provider = provider or LocalShellProvider(cwd or ".")

    def run(self, pid: str, argv: list[str], timeout: float = 30.0) -> CommandResult:
        if not argv:
            raise ValueError("argv cannot be empty")
        self.capabilities.require(pid, f"shell:{argv[0]}", CapabilityRight.EXECUTE)
        proc = self.provider.run(argv, timeout=timeout)
        self.audit.record(
            actor=pid,
            action="external.shell.run",
            target=f"shell:{argv[0]}",
            decision={"argv": argv, "returncode": proc.returncode},
        )
        return proc

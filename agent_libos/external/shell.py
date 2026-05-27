from __future__ import annotations

import subprocess
from pathlib import Path

from agent_libos.capability.manager import CapabilityManager
from agent_libos.models import CapabilityRight
from agent_libos.runtime.audit_manager import AuditManager


class ShellAdapter:
    def __init__(self, capabilities: CapabilityManager, audit: AuditManager, cwd: str | Path):
        self.capabilities = capabilities
        self.audit = audit
        self.cwd = Path(cwd)

    def run(self, pid: str, argv: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        if not argv:
            raise ValueError("argv cannot be empty")
        self.capabilities.require(pid, f"shell:{argv[0]}", CapabilityRight.EXECUTE)
        proc = subprocess.run(argv, cwd=self.cwd, text=True, capture_output=True, timeout=timeout)
        self.audit.record(
            actor=pid,
            action="external.shell.run",
            target=f"shell:{argv[0]}",
            decision={"argv": argv, "returncode": proc.returncode},
        )
        return proc


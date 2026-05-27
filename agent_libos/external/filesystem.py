from __future__ import annotations

from pathlib import Path

from agent_libos.capability.manager import CapabilityManager
from agent_libos.models import CapabilityRight
from agent_libos.runtime.audit_manager import AuditManager


class FilesystemAdapter:
    def __init__(self, capabilities: CapabilityManager, audit: AuditManager, root: str | Path):
        self.capabilities = capabilities
        self.audit = audit
        self.root = Path(root).resolve()

    def read_text(self, pid: str, path: str | Path, encoding: str = "utf-8") -> str:
        target = self._resolve(path)
        self.capabilities.require(pid, self._resource(target), CapabilityRight.READ)
        text = target.read_text(encoding=encoding)
        self.audit.record(actor=pid, action="external.filesystem.read_text", target=str(target))
        return text

    def write_text(self, pid: str, path: str | Path, text: str, encoding: str = "utf-8") -> None:
        target = self._resolve(path)
        self.capabilities.require(pid, self._resource(target), CapabilityRight.WRITE)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding=encoding)
        self.audit.record(actor=pid, action="external.filesystem.write_text", target=str(target))

    def _resolve(self, path: str | Path) -> Path:
        target = (self.root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if self.root not in target.parents and target != self.root:
            raise PermissionError(f"path escapes adapter root: {path}")
        return target

    def _resource(self, path: Path) -> str:
        return f"filesystem:{path}"


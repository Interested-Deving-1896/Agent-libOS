from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from agent_libos.capability.manager import CapabilityManager
from agent_libos.exceptions import CapabilityDenied, NotFound
from agent_libos.models import Capability, CapabilityRight, EventType
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus


@dataclass(frozen=True)
class FileReadResult:
    path: str
    content: str
    bytes_read: int
    truncated: bool


@dataclass(frozen=True)
class FileWriteResult:
    path: str
    bytes_written: int
    created: bool


class FilesystemAdapter:
    def __init__(
        self,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        root: str | Path,
        namespace: str = "workspace",
    ):
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        self.root = Path(root).resolve()
        self.namespace = namespace

    def read_text(
        self,
        pid: str,
        path: str | Path,
        encoding: str = "utf-8",
        max_bytes: int = 65536,
    ) -> FileReadResult:
        target, relative = self._resolve(path)
        resource = self.resource_for(relative)
        self.capabilities.require(pid, resource, CapabilityRight.READ)
        if not target.exists():
            raise NotFound(f"file does not exist: {relative}")
        if not target.is_file():
            raise CapabilityDenied(f"path is not a file: {relative}")
        raw = target.read_bytes()
        truncated = len(raw) > max_bytes
        selected = raw[:max_bytes]
        content = selected.decode(encoding)
        self.events.emit(
            EventType.EXTERNAL_READ,
            source=pid,
            target=resource,
            payload={"adapter": "filesystem", "path": relative, "bytes_read": len(selected), "truncated": truncated},
        )
        self.audit.record(
            actor=pid,
            action="external.filesystem.read_text",
            target=resource,
            decision={"path": relative, "bytes_read": len(selected), "truncated": truncated},
        )
        return FileReadResult(path=relative, content=content, bytes_read=len(selected), truncated=truncated)

    def write_text(
        self,
        pid: str,
        path: str | Path,
        text: str,
        encoding: str = "utf-8",
        overwrite: bool = True,
    ) -> FileWriteResult:
        target, relative = self._resolve(path)
        resource = self.resource_for(relative)
        self.capabilities.require(pid, resource, CapabilityRight.WRITE)
        created = not target.exists()
        if target.exists() and not target.is_file():
            raise CapabilityDenied(f"path is not a file: {relative}")
        if target.exists() and not overwrite:
            raise FileExistsError(f"file already exists: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding=encoding)
        bytes_written = len(text.encode(encoding))
        self.events.emit(
            EventType.EXTERNAL_WRITE,
            source=pid,
            target=resource,
            payload={"adapter": "filesystem", "path": relative, "bytes_written": bytes_written, "created": created},
        )
        self.audit.record(
            actor=pid,
            action="external.filesystem.write_text",
            target=resource,
            decision={"path": relative, "bytes_written": bytes_written, "created": created},
        )
        return FileWriteResult(path=relative, bytes_written=bytes_written, created=created)

    def grant_workspace(
        self,
        pid: str,
        rights: Iterable[str | CapabilityRight],
        issued_by: str = "filesystem",
    ) -> Capability:
        return self.capabilities.grant(
            subject=pid,
            resource=self.workspace_resource(),
            rights=rights,
            issued_by=issued_by,
        )

    def grant_path(
        self,
        pid: str,
        path: str | Path,
        rights: Iterable[str | CapabilityRight],
        issued_by: str = "filesystem",
    ) -> Capability:
        _target, relative = self._resolve(path)
        return self.capabilities.grant(
            subject=pid,
            resource=self.resource_for(relative),
            rights=rights,
            issued_by=issued_by,
        )

    def workspace_resource(self) -> str:
        return f"filesystem:{self.namespace}:*"

    def resource_for(self, path: str | Path) -> str:
        relative = Path(path).as_posix()
        if relative in {"", "."}:
            return f"filesystem:{self.namespace}:"
        return f"filesystem:{self.namespace}:{relative}"

    def _resolve(self, path: str | Path) -> tuple[Path, str]:
        target = (self.root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if self.root not in target.parents and target != self.root:
            raise CapabilityDenied(f"path escapes filesystem adapter root: {path}")
        relative = target.relative_to(self.root).as_posix()
        return target, relative

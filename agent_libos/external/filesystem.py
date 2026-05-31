from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agent_libos.capability.manager import CapabilityManager
from agent_libos.exceptions import CapabilityDenied, HumanApprovalRequired, NotFound
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
    """Workspace-contained filesystem primitive."""

    def __init__(
        self,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        root: str | Path,
        namespace: str = "workspace",
        human: Any | None = None,
    ):
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        self.root = Path(root).resolve()
        self.namespace = namespace
        self.human = human

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
        consumed_once = self._require_write(
            pid=pid,
            resource=resource,
            target=target,
            relative=relative,
            text=text,
            encoding=encoding,
            overwrite=overwrite,
        )
        created = not target.exists()
        if target.exists() and not target.is_file():
            raise CapabilityDenied(f"path is not a file: {relative}")
        if target.exists() and not overwrite:
            raise FileExistsError(f"file already exists: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding=encoding, newline="\n")
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
        if consumed_once:
            self.capabilities.consume_allow_once(
                subject=pid,
                resource=resource,
                right=CapabilityRight.WRITE,
                used_by="filesystem",
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

    def _require_write(
        self,
        pid: str,
        resource: str,
        target: Path,
        relative: str,
        text: str,
        encoding: str,
        overwrite: bool,
    ) -> bool:
        policy = self.capabilities.permission_policy(pid, resource, CapabilityRight.WRITE)
        if policy == CapabilityManager.ALWAYS_ALLOW:
            return False
        if policy == CapabilityManager.ALLOW_ONCE:
            return True
        if policy == CapabilityManager.ALWAYS_DENY:
            raise CapabilityDenied(f"{pid} denied write on {resource}")
        if policy == CapabilityManager.ASK_EACH_TIME:
            if self.human is None:
                raise CapabilityDenied(f"{pid} requires human approval for write on {resource}")
            # This primitive has the concrete path, overwrite state, byte count,
            # and preview needed for a safe per-use human decision.
            request_id = self.human.query(
                pid=pid,
                human="owner",
                request={
                    "type": "external_operation_approval",
                    "question": f"Allow this process to write {relative}?",
                    "requested_once_capability": {
                        "subject": pid,
                        "resource": resource,
                        "rights": [CapabilityRight.WRITE.value],
                    },
                    "context": {
                        **self._write_approval_context(
                            pid=pid,
                            resource=resource,
                            target=target,
                            relative=relative,
                            text=text,
                            encoding=encoding,
                            overwrite=overwrite,
                        ),
                    },
                },
                blocking=True,
            )
            raise HumanApprovalRequired(
                request_id=request_id,
                message=f"{pid} is waiting for per-use human approval to write {resource}",
            )
        raise CapabilityDenied(f"{pid} lacks write on {resource}")

    def _write_approval_context(
        self,
        pid: str,
        resource: str,
        target: Path,
        relative: str,
        text: str,
        encoding: str,
        overwrite: bool,
    ) -> dict[str, Any]:
        encoded = text.encode(encoding)
        preview, preview_truncated = self._preview_text(text)
        target_state = self._target_state(target)
        will_overwrite = bool(target_state["exists"] and target_state["kind"] == "file")
        return {
            "adapter": "filesystem",
            "primitive": "runtime.filesystem.write_text",
            "operation": "write_text",
            "pid": pid,
            "workspace_root": str(self.root),
            "path": relative,
            "absolute_path": str(target),
            "resource": resource,
            "right": CapabilityRight.WRITE.value,
            "grant_scope": "one_time",
            "encoding": encoding,
            "overwrite": overwrite,
            "will_create": not target_state["exists"],
            "will_overwrite": will_overwrite,
            "content_bytes": len(encoded),
            "content_sha256": hashlib.sha256(encoded).hexdigest(),
            "content_preview": preview,
            "content_preview_chars": len(preview),
            "content_preview_truncated": preview_truncated,
            "target": target_state,
        }

    def _preview_text(self, text: str, limit: int = 256) -> tuple[str, bool]:
        preview = text[:limit]
        # repr() prevents newlines or prompt-like text from masquerading as
        # separate approval instructions in the human terminal prompt.
        return repr(preview), len(text) > limit

    def _target_state(self, target: Path) -> dict[str, Any]:
        if not target.exists():
            return {"exists": False, "kind": "missing"}
        stat = target.stat()
        kind = "file" if target.is_file() else "directory" if target.is_dir() else "other"
        return {
            "exists": True,
            "kind": kind,
            "size_bytes": stat.st_size if target.is_file() else None,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }

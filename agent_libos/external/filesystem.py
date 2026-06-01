from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Iterable

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models.exceptions import CapabilityDenied, HumanApprovalRequired, NotFound
from agent_libos.models import Capability, CapabilityRight, EventType
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.substrate import FilesystemProvider, LocalFilesystemProvider, ResolvedPath

_RUNTIME_DEFAULTS = DEFAULT_CONFIG.runtime
_TOOL_DEFAULTS = DEFAULT_CONFIG.tools


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


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    path: str
    kind: str
    size_bytes: int | None
    modified_at: str


@dataclass(frozen=True)
class DirectoryReadResult:
    path: str
    entries: list[DirectoryEntry]
    count: int
    truncated: bool


@dataclass(frozen=True)
class DirectoryWriteResult:
    path: str
    created: bool


@dataclass(frozen=True)
class DeleteResult:
    path: str
    kind: str
    deleted: bool
    recursive: bool = False


class FilesystemAdapter:
    """Workspace-contained filesystem primitive."""

    def __init__(
        self,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        root: str | os.PathLike[str] | None = None,
        namespace: str = _RUNTIME_DEFAULTS.workspace_namespace,
        human: Any | None = None,
        provider: FilesystemProvider | None = None,
    ):
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        if provider is None:
            if root is None:
                raise ValueError("FilesystemAdapter requires either root or provider")
            provider = LocalFilesystemProvider(root, namespace=namespace)
        self.provider = provider
        self.root = provider.root_display
        self.namespace = provider.namespace
        self.human = human

    def read_text(
        self,
        pid: str,
        path: str | os.PathLike[str],
        encoding: str = _TOOL_DEFAULTS.default_text_encoding,
        max_bytes: int = _TOOL_DEFAULTS.filesystem_read_max_bytes,
    ) -> FileReadResult:
        target, relative = self._resolve(path)
        resource = self.resource_for(relative)
        self.capabilities.require(pid, resource, CapabilityRight.READ)
        target_state = self.provider.state(target)
        if not target_state.exists:
            raise NotFound(f"file does not exist: {relative}")
        if target_state.kind != "file":
            raise CapabilityDenied(f"path is not a file: {relative}")
        raw = self.provider.read_bytes(target)
        truncated = len(raw) > max_bytes
        selected = raw[:max_bytes]
        content = self._decode_text_prefix(selected, encoding, truncated=truncated)
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
        path: str | os.PathLike[str],
        text: str,
        encoding: str = _TOOL_DEFAULTS.default_text_encoding,
        overwrite: bool = True,
    ) -> FileWriteResult:
        target, relative = self._resolve(path)
        resource = self.resource_for(relative)
        self._reject_definite_permission_denial(pid, resource, CapabilityRight.WRITE)
        target_state = self.provider.state(target)
        if target_state.exists and target_state.kind != "file":
            raise CapabilityDenied(f"path is not a file: {relative}")
        if target_state.exists and not overwrite:
            raise FileExistsError(f"file already exists: {relative}")
        consumed_once = self._require_write(
            pid=pid,
            resource=resource,
            target=target,
            relative=relative,
            text=text,
            encoding=encoding,
            overwrite=overwrite,
        )
        try:
            target_state = self.provider.state(target)
            created = not target_state.exists
            if target_state.exists and target_state.kind != "file":
                raise CapabilityDenied(f"path is not a file: {relative}")
            if target_state.exists and not overwrite:
                raise FileExistsError(f"file already exists: {relative}")
            self.provider.write_text(target, text, encoding=encoding, newline="\n")
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
        finally:
            if consumed_once:
                self.capabilities.consume_allow_once(
                    subject=pid,
                    resource=resource,
                    right=CapabilityRight.WRITE,
                    used_by="filesystem",
                )
        return FileWriteResult(path=relative, bytes_written=bytes_written, created=created)

    def read_directory(
        self,
        pid: str,
        path: str | os.PathLike[str],
        limit: int = _TOOL_DEFAULTS.directory_entry_limit,
    ) -> DirectoryReadResult:
        target, relative = self._resolve(path)
        resource = self.directory_resource_for(relative)
        self.capabilities.require(pid, resource, CapabilityRight.READ)
        target_state = self.provider.state(target)
        if not target_state.exists:
            raise NotFound(f"directory does not exist: {relative}")
        if target_state.kind != "directory":
            raise CapabilityDenied(f"path is not a directory: {relative}")
        children = list(self.provider.list_directory(target))
        selected = children[:limit]
        entries = [DirectoryEntry(**entry.__dict__) for entry in selected]
        truncated = len(children) > len(selected)
        self.events.emit(
            EventType.EXTERNAL_READ,
            source=pid,
            target=resource,
            payload={
                "adapter": "filesystem",
                "operation": "read_directory",
                "path": relative,
                "count": len(entries),
                "truncated": truncated,
            },
        )
        self.audit.record(
            actor=pid,
            action="external.filesystem.read_directory",
            target=resource,
            decision={"path": relative, "count": len(entries), "truncated": truncated},
        )
        return DirectoryReadResult(path=relative, entries=entries, count=len(entries), truncated=truncated)

    def write_directory(
        self,
        pid: str,
        path: str | os.PathLike[str],
        parents: bool = True,
        exist_ok: bool = True,
    ) -> DirectoryWriteResult:
        target, relative = self._resolve(path)
        resource = self.directory_resource_for(relative)
        self._reject_definite_permission_denial(pid, resource, CapabilityRight.WRITE)
        target_state = self.provider.state(target)
        if target_state.exists and target_state.kind != "directory":
            raise CapabilityDenied(f"path is not a directory: {relative}")
        if target_state.exists and not exist_ok:
            raise FileExistsError(f"directory already exists: {relative}")
        consumed_once = self._require_write_operation(
            pid=pid,
            resource=resource,
            target=target,
            relative=relative,
            operation="write_directory",
            primitive="runtime.filesystem.write_directory",
            question=f"Allow this process to create or update directory {relative}?",
            extra_context={"parents": parents, "exist_ok": exist_ok},
        )
        try:
            target_state = self.provider.state(target)
            created = not target_state.exists
            if target_state.exists and target_state.kind != "directory":
                raise CapabilityDenied(f"path is not a directory: {relative}")
            if target_state.exists and not exist_ok:
                raise FileExistsError(f"directory already exists: {relative}")
            self.provider.make_directory(target, parents=parents, exist_ok=exist_ok)
            self.events.emit(
                EventType.EXTERNAL_WRITE,
                source=pid,
                target=resource,
                payload={"adapter": "filesystem", "operation": "write_directory", "path": relative, "created": created},
            )
            self.audit.record(
                actor=pid,
                action="external.filesystem.write_directory",
                target=resource,
                decision={"path": relative, "created": created, "parents": parents, "exist_ok": exist_ok},
            )
        finally:
            if consumed_once:
                self.capabilities.consume_allow_once(
                    subject=pid,
                    resource=resource,
                    right=CapabilityRight.WRITE,
                    used_by="filesystem",
                )
        return DirectoryWriteResult(path=relative, created=created)

    def delete_file(
        self,
        pid: str,
        path: str | os.PathLike[str],
        missing_ok: bool = False,
    ) -> DeleteResult:
        target, relative = self._resolve(path)
        resource = self.resource_for(relative)
        self._reject_definite_permission_denial(pid, resource, CapabilityRight.DELETE)
        target_state = self.provider.state(target)
        if not target_state.exists and not missing_ok:
            raise NotFound(f"file does not exist: {relative}")
        if target_state.exists and target_state.kind != "file":
            raise CapabilityDenied(f"path is not a file: {relative}")
        consumed_once = self._require_delete(
            pid=pid,
            resource=resource,
            target=target,
            relative=relative,
            operation="delete_file",
            recursive=False,
            missing_ok=missing_ok,
        )
        try:
            target_state = self.provider.state(target)
            if not target_state.exists:
                if not missing_ok:
                    raise NotFound(f"file does not exist: {relative}")
                return DeleteResult(path=relative, kind="missing", deleted=False)
            if target_state.kind != "file":
                raise CapabilityDenied(f"path is not a file: {relative}")
            self.provider.delete_file(target)
            self.events.emit(
                EventType.EXTERNAL_WRITE,
                source=pid,
                target=resource,
                payload={"adapter": "filesystem", "operation": "delete_file", "path": relative},
            )
            self.audit.record(
                actor=pid,
                action="external.filesystem.delete_file",
                target=resource,
                decision={"path": relative, "deleted": True},
            )
            return DeleteResult(path=relative, kind="file", deleted=True)
        finally:
            if consumed_once:
                self.capabilities.consume_allow_once(
                    subject=pid,
                    resource=resource,
                    right=CapabilityRight.DELETE,
                    used_by="filesystem",
                )

    def delete_directory(
        self,
        pid: str,
        path: str | os.PathLike[str],
        recursive: bool = False,
        missing_ok: bool = False,
    ) -> DeleteResult:
        target, relative = self._resolve(path)
        if target.is_root:
            raise CapabilityDenied("cannot delete filesystem adapter root")
        resource = self.directory_resource_for(relative)
        self._reject_definite_permission_denial(pid, resource, CapabilityRight.DELETE)
        target_state = self.provider.state(target)
        if not target_state.exists and not missing_ok:
            raise NotFound(f"directory does not exist: {relative}")
        if target_state.exists and target_state.kind != "directory":
            raise CapabilityDenied(f"path is not a directory: {relative}")
        consumed_once = self._require_delete(
            pid=pid,
            resource=resource,
            target=target,
            relative=relative,
            operation="delete_directory",
            recursive=recursive,
            missing_ok=missing_ok,
        )
        try:
            target_state = self.provider.state(target)
            if not target_state.exists:
                if not missing_ok:
                    raise NotFound(f"directory does not exist: {relative}")
                return DeleteResult(path=relative, kind="missing", deleted=False, recursive=recursive)
            if target_state.kind != "directory":
                raise CapabilityDenied(f"path is not a directory: {relative}")
            self.provider.delete_directory(target, recursive=recursive)
            self.events.emit(
                EventType.EXTERNAL_WRITE,
                source=pid,
                target=resource,
                payload={
                    "adapter": "filesystem",
                    "operation": "delete_directory",
                    "path": relative,
                    "recursive": recursive,
                },
            )
            self.audit.record(
                actor=pid,
                action="external.filesystem.delete_directory",
                target=resource,
                decision={"path": relative, "deleted": True, "recursive": recursive},
            )
            return DeleteResult(path=relative, kind="directory", deleted=True, recursive=recursive)
        finally:
            if consumed_once:
                self.capabilities.consume_allow_once(
                    subject=pid,
                    resource=resource,
                    right=CapabilityRight.DELETE,
                    used_by="filesystem",
                )

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
        path: str | os.PathLike[str],
        rights: Iterable[str | CapabilityRight],
        issued_by: str = "filesystem",
    ) -> Capability:
        return self.capabilities.grant(
            subject=pid,
            resource=self.resource_for_path(path),
            rights=rights,
            issued_by=issued_by,
        )

    def grant_directory(
        self,
        pid: str,
        path: str | os.PathLike[str],
        rights: Iterable[str | CapabilityRight],
        issued_by: str = "filesystem",
    ) -> Capability:
        return self.capabilities.grant(
            subject=pid,
            resource=self.directory_resource_for_path(path),
            rights=rights,
            issued_by=issued_by,
        )

    def grant_path_list(
        self,
        pid: str,
        *,
        read_files: Iterable[str | os.PathLike[str]] = (),
        write_files: Iterable[str | os.PathLike[str]] = (),
        delete_files: Iterable[str | os.PathLike[str]] = (),
        read_dirs: Iterable[str | os.PathLike[str]] = (),
        write_dirs: Iterable[str | os.PathLike[str]] = (),
        delete_dirs: Iterable[str | os.PathLike[str]] = (),
        issued_by: str = "filesystem",
    ) -> list[Capability]:
        grants: list[Capability] = []
        for path in read_files:
            grants.append(self.grant_path(pid, path, [CapabilityRight.READ], issued_by=issued_by))
        for path in write_files:
            grants.append(self.grant_path(pid, path, [CapabilityRight.WRITE], issued_by=issued_by))
        for path in delete_files:
            grants.append(self.grant_path(pid, path, [CapabilityRight.DELETE], issued_by=issued_by))
        for path in read_dirs:
            grants.append(self.grant_directory(pid, path, [CapabilityRight.READ], issued_by=issued_by))
        for path in write_dirs:
            grants.append(self.grant_directory(pid, path, [CapabilityRight.WRITE], issued_by=issued_by))
        for path in delete_dirs:
            grants.append(self.grant_directory(pid, path, [CapabilityRight.DELETE], issued_by=issued_by))
        return grants

    def workspace_resource(self) -> str:
        return f"filesystem:{self.namespace}:*"

    def resource_for(self, path: str | os.PathLike[str]) -> str:
        relative = self._logical_path(path)
        if relative in {"", "."}:
            return f"filesystem:{self.namespace}:"
        return f"filesystem:{self.namespace}:{relative}"

    def resource_for_path(self, path: str | os.PathLike[str]) -> str:
        _target, relative = self._resolve(path)
        return self.resource_for(relative)

    def directory_resource_for(self, path: str | os.PathLike[str]) -> str:
        relative = self._logical_path(path).rstrip("/")
        if relative in {"", "."}:
            return self.workspace_resource()
        return f"filesystem:{self.namespace}:{relative}/*"

    def directory_resource_for_path(self, path: str | os.PathLike[str]) -> str:
        _target, relative = self._resolve(path)
        return self.directory_resource_for(relative)

    def _resolve(self, path: str | os.PathLike[str]) -> tuple[ResolvedPath, str]:
        target = self.provider.resolve(path)
        return target, target.relative

    def _logical_path(self, path: str | os.PathLike[str]) -> str:
        return os.fspath(path).replace("\\", "/")

    def _require_write(
        self,
        pid: str,
        resource: str,
        target: ResolvedPath,
        relative: str,
        text: str,
        encoding: str,
        overwrite: bool,
    ) -> bool:
        return self._require_write_operation(
            pid=pid,
            resource=resource,
            target=target,
            relative=relative,
            operation="write_text",
            primitive="runtime.filesystem.write_text",
            question=f"Allow this process to write {relative}?",
            extra_context={
                "encoding": encoding,
                "overwrite": overwrite,
                **self._content_context(text, encoding),
            },
        )

    def _reject_definite_permission_denial(
        self,
        pid: str,
        resource: str,
        right: CapabilityRight,
    ) -> None:
        # Do not stat the target before a definite deny/miss; existence and
        # kind are filesystem facts that require some matching policy first.
        policy = self.capabilities.permission_policy(pid, resource, right)
        if policy in {CapabilityManager.MISSING, CapabilityManager.ALWAYS_DENY}:
            self.capabilities.require(pid, resource, right)

    def _require_write_operation(
        self,
        pid: str,
        resource: str,
        target: ResolvedPath,
        relative: str,
        operation: str,
        primitive: str,
        question: str,
        extra_context: dict[str, Any] | None = None,
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
                human=_RUNTIME_DEFAULTS.default_human,
                request={
                    "type": "external_operation_approval",
                    "question": question,
                    "requested_once_capability": {
                        "subject": pid,
                        "resource": resource,
                        "rights": [CapabilityRight.WRITE.value],
                    },
                    "context": {
                        **self._operation_context(
                            pid=pid,
                            resource=resource,
                            target=target,
                            relative=relative,
                            primitive=primitive,
                            operation=operation,
                            right=CapabilityRight.WRITE.value,
                            extra=extra_context or {},
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

    def _require_delete(
        self,
        pid: str,
        resource: str,
        target: ResolvedPath,
        relative: str,
        operation: str,
        recursive: bool,
        missing_ok: bool,
    ) -> bool:
        policy = self.capabilities.permission_policy(pid, resource, CapabilityRight.DELETE)
        if policy == CapabilityManager.ALWAYS_ALLOW:
            return False
        if policy == CapabilityManager.ALLOW_ONCE:
            return True
        if policy == CapabilityManager.ALWAYS_DENY:
            raise CapabilityDenied(f"{pid} denied delete on {resource}")
        if policy == CapabilityManager.ASK_EACH_TIME:
            if self.human is None:
                raise CapabilityDenied(f"{pid} requires human approval for delete on {resource}")
            request_id = self.human.query(
                pid=pid,
                human=_RUNTIME_DEFAULTS.default_human,
                request={
                    "type": "external_operation_approval",
                    "question": f"Allow this process to delete {relative}?",
                    "requested_once_capability": {
                        "subject": pid,
                        "resource": resource,
                        "rights": [CapabilityRight.DELETE.value],
                    },
                    "context": self._operation_context(
                        pid=pid,
                        resource=resource,
                        target=target,
                        relative=relative,
                        primitive=f"runtime.filesystem.{operation}",
                        operation=operation,
                        right=CapabilityRight.DELETE.value,
                        extra={"recursive": recursive, "missing_ok": missing_ok},
                    ),
                },
                blocking=True,
            )
            raise HumanApprovalRequired(
                request_id=request_id,
                message=f"{pid} is waiting for per-use human approval to delete {resource}",
            )
        raise CapabilityDenied(f"{pid} lacks delete on {resource}")

    def _operation_context(
        self,
        pid: str,
        resource: str,
        target: ResolvedPath,
        relative: str,
        primitive: str,
        operation: str,
        right: str,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        target_state = self._target_state(target)
        will_overwrite = bool(target_state["exists"] and target_state["kind"] == "file")
        return {
            "adapter": "filesystem",
            "primitive": primitive,
            "operation": operation,
            "pid": pid,
            "workspace_root": self.root,
            "path": relative,
            "absolute_path": target.display,
            "resource": resource,
            "right": right,
            "grant_scope": "one_time",
            "will_create": not target_state["exists"],
            "will_overwrite": will_overwrite,
            "target": target_state,
            **extra,
        }

    def _content_context(self, text: str, encoding: str) -> dict[str, Any]:
        encoded = text.encode(encoding)
        preview, preview_truncated = self._preview_text(text)
        return {
            "content_bytes": len(encoded),
            "content_sha256": hashlib.sha256(encoded).hexdigest(),
            "content_preview": preview,
            "content_preview_chars": len(preview),
            "content_preview_truncated": preview_truncated,
        }

    def _preview_text(self, text: str, limit: int = _TOOL_DEFAULTS.approval_preview_chars) -> tuple[str, bool]:
        preview = text[:limit]
        # repr() prevents newlines or prompt-like text from masquerading as
        # separate approval instructions in the human terminal prompt.
        return repr(preview), len(text) > limit

    def _decode_text_prefix(self, data: bytes, encoding: str, *, truncated: bool) -> str:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as exc:
            if truncated and exc.end == len(data):
                return data[: exc.start].decode(encoding)
            raise

    def _target_state(self, target: ResolvedPath) -> dict[str, Any]:
        state = self.provider.state(target)
        if not state.exists:
            return {"exists": False, "kind": "missing"}
        result = {
            "exists": True,
            "kind": state.kind,
            "size_bytes": state.size_bytes,
            "modified_at": state.modified_at,
        }
        return result

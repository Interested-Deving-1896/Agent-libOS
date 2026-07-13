from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote

from agent_libos.capability.manager import CapabilityManager
from agent_libos.capability.rules import AUTHORITY_RULES_KEY
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models.exceptions import CapabilityDenied, HumanApprovalRequired, NotFound, ValidationError
from agent_libos.models import (
    AuthorityRisk,
    Capability,
    CapabilityDecision,
    CapabilityEffect,
    CapabilityRight,
    EventType,
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    ResourceUsage,
)
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.substrate import (
    FilesystemProvider,
    LocalFilesystemProvider,
    ResolvedPath,
)
from agent_libos.sdk import (
    ProtectedOperationEvidence,
    ProtectedOperationInvocation,
    ProviderPhase,
    ResourceSettlement,
)

_RUNTIME_DEFAULTS = DEFAULT_CONFIG.runtime
_TOOL_DEFAULTS = DEFAULT_CONFIG.tools
_RESOURCE_SEGMENT_SAFE = "-._~"
_DIRECTORY_STATE_OBSERVATION_BYTES = 512


@dataclass(frozen=True)
class FileReadResult:
    path: str
    content: str
    bytes_read: int
    truncated: bool


@dataclass(frozen=True)
class FileBytesReadResult:
    path: str
    content: bytes
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
        resources: Any | None = None,
        config: AgentLibOSConfig | None = None,
    ):
        self.config = config or DEFAULT_CONFIG
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
        self.resources = resources

    def validate_directory(
        self,
        pid: str,
        path: str | os.PathLike[str],
        cwd: str | os.PathLike[str] | None = None,
    ) -> str:
        """Authorize and validate one directory-state observation.

        Working-directory selection needs host filesystem metadata, so it must
        cross the same capability, finite-use, resource, audit/event, and
        external-effect boundary as other filesystem reads.  The returned path
        is the normalized lexical workspace-relative path; provider state/sink
        checks resolve real paths only after authorization.
        """

        target, relative = self._resolve(path, cwd=cwd)
        resource = self.directory_resource_for(relative)
        authority_context = self._authorization_context(
            pid=pid,
            resource=resource,
            relative=relative,
            primitive="runtime.filesystem.validate_directory",
            operation="state",
            right=CapabilityRight.READ.value,
        )
        decision = self.capabilities.require(
            pid, resource, CapabilityRight.READ, authority_context, consume=False
        )
        effect_context = {
            "path": relative,
            "resource": resource,
            "expected_kind": "directory",
        }
        usage = ResourceUsage(external_read_bytes=_DIRECTORY_STATE_OBSERVATION_BYTES)
        invocation = ProtectedOperationInvocation(
            pid=pid,
            actor=pid,
            target=resource,
            decisions=(decision,),
            canonical_args=authority_context,
            observation=effect_context,
            preflight_usage=usage,
            resource_source="primitive.filesystem.validate_directory",
            resource_context=effect_context,
            failure_evidence=lambda error, phase: self._protected_failure_evidence(
                pid, resource, "primitive.filesystem.validate_directory.failed", effect_context, error, phase
            ),
        )
        with self._protected().start(
            "primitive.filesystem.validate_directory", invocation, provider=self.provider
        ) as protected:
            state = protected.call(
                ProviderPhase("state", information_flow=True), self.provider.state, target
            )
            error: Exception | None = None
            if not state.exists:
                outcome = "not_found"
                error = NotFound(f"working directory does not exist: {relative}")
            elif state.kind != "directory":
                outcome = "not_directory"
                error = NotFound(f"working directory is not a directory: {relative}")
            else:
                outcome = "validated"
            result_payload = {"outcome": outcome, "state_kind": state.kind}
            protected.complete(
                state,
                self._protected_filesystem_evidence(
                    pid,
                    resource,
                    EventType.EXTERNAL_READ,
                    "primitive.filesystem.validate_directory",
                    {"adapter": "filesystem", "operation": "state", "path": relative, **result_payload},
                    {"path": relative, "state_kind": state.kind, **result_payload},
                    result_payload,
                ),
                classification_context=effect_context,
                classification_result=result_payload,
                resource=ResourceSettlement(
                    usage=usage,
                    source="primitive.filesystem.validate_directory",
                    context=effect_context,
                ),
            )
            if error is not None:
                raise error
            return relative or "."

    def read_text(
        self,
        pid: str,
        path: str | os.PathLike[str],
        encoding: str = _TOOL_DEFAULTS.default_text_encoding,
        max_bytes: int = _TOOL_DEFAULTS.filesystem_read_max_bytes,
        cwd: str | os.PathLike[str] | None = None,
    ) -> FileReadResult:
        max_bytes = self._bounded_positive_int(
            max_bytes,
            label="max_bytes",
            hard_limit=self.config.tools.filesystem_read_hard_limit_bytes,
        )
        target, relative = self._resolve(path, cwd=cwd)
        resource = self.resource_for(relative)
        authority_context = self._authorization_context(
            pid=pid,
            resource=resource,
            relative=relative,
            primitive="runtime.filesystem.read_text",
            operation="read_text",
            right=CapabilityRight.READ.value,
            extra={"max_bytes": max_bytes, "encoding": encoding},
        )
        decision = self.capabilities.require(
            pid, resource, CapabilityRight.READ, authority_context, consume=False
        )
        effect_context = {"path": relative, "resource": resource, "encoding": encoding, "max_bytes": max_bytes}
        invocation = ProtectedOperationInvocation(
            pid=pid,
            actor=pid,
            target=resource,
            decisions=(decision,),
            canonical_args=authority_context,
            observation=effect_context,
            preflight_usage=ResourceUsage(external_read_bytes=max_bytes),
            resource_source="primitive.filesystem.read_text",
            resource_context=effect_context,
            failure_evidence=lambda error, phase: self._protected_failure_evidence(
                pid, resource, "primitive.filesystem.read_text.failed", effect_context, error, phase
            ),
        )
        with self._protected().start("primitive.filesystem.read_text", invocation, provider=self.provider) as protected:
            target_state = protected.call(
                ProviderPhase("state", information_flow=True), self.provider.state, target
            )
            if not target_state.exists:
                error = NotFound(f"file does not exist: {relative}")
                self._complete_state_rejection(
                    protected,
                    pid=pid,
                    target=resource,
                    audit_action="primitive.filesystem.read_text.rejected",
                    context=effect_context,
                    error=error,
                    resource_source="primitive.filesystem.read_text",
                )
                raise error
            if target_state.kind != "file":
                error = CapabilityDenied(f"path is not a file: {relative}")
                self._complete_state_rejection(
                    protected,
                    pid=pid,
                    target=resource,
                    audit_action="primitive.filesystem.read_text.rejected",
                    context=effect_context,
                    error=error,
                    resource_source="primitive.filesystem.read_text",
                )
                raise error
            read_limit = self._read_limit_for_state(target_state.size_bytes, max_bytes)
            raw = protected.call(
                ProviderPhase("read", information_flow=True),
                self._provider_read_bytes,
                target,
                max_bytes=read_limit,
            )
            truncated = self._is_truncated_read(target_state.size_bytes, len(raw), max_bytes)
            selected = raw[:max_bytes]
            content = self._decode_text_prefix(selected, encoding, truncated=truncated)
            result = FileReadResult(
                path=relative, content=content, bytes_read=len(selected), truncated=truncated
            )
            result_payload = {"bytes_read": len(selected), "truncated": truncated}
            return protected.complete(
                result,
                self._protected_filesystem_evidence(
                    pid,
                    resource,
                    EventType.EXTERNAL_READ,
                    "primitive.filesystem.read_text",
                    {"adapter": "filesystem", "path": relative, **result_payload},
                    {"path": relative, **result_payload},
                    result_payload,
                ),
                classification_context=effect_context,
                classification_result=result_payload,
                resource=ResourceSettlement(
                    usage=ResourceUsage(external_read_bytes=len(selected)),
                    source="primitive.filesystem.read_text",
                    context=effect_context,
                ),
            )

    def read_bytes(
        self,
        pid: str,
        path: str | os.PathLike[str],
        max_bytes: int = _TOOL_DEFAULTS.filesystem_read_max_bytes,
        cwd: str | os.PathLike[str] | None = None,
    ) -> FileBytesReadResult:
        max_bytes = self._bounded_positive_int(
            max_bytes,
            label="max_bytes",
            hard_limit=self.config.tools.filesystem_read_hard_limit_bytes,
        )
        target, relative = self._resolve(path, cwd=cwd)
        resource = self.resource_for(relative)
        authority_context = self._authorization_context(
            pid=pid,
            resource=resource,
            relative=relative,
            primitive="runtime.filesystem.read_bytes",
            operation="read_bytes",
            right=CapabilityRight.READ.value,
            extra={"max_bytes": max_bytes},
        )
        decision = self.capabilities.require(
            pid, resource, CapabilityRight.READ, authority_context, consume=False
        )
        effect_context = {"path": relative, "resource": resource, "max_bytes": max_bytes}
        invocation = ProtectedOperationInvocation(
            pid=pid,
            actor=pid,
            target=resource,
            decisions=(decision,),
            canonical_args=authority_context,
            observation=effect_context,
            preflight_usage=ResourceUsage(external_read_bytes=max_bytes),
            resource_source="primitive.filesystem.read_bytes",
            resource_context=effect_context,
            failure_evidence=lambda error, phase: self._protected_failure_evidence(
                pid, resource, "primitive.filesystem.read_bytes.failed", effect_context, error, phase
            ),
        )
        with self._protected().start("primitive.filesystem.read_bytes", invocation, provider=self.provider) as protected:
            target_state = protected.call(
                ProviderPhase("state", information_flow=True), self.provider.state, target
            )
            if not target_state.exists:
                error = NotFound(f"file does not exist: {relative}")
                self._complete_state_rejection(
                    protected,
                    pid=pid,
                    target=resource,
                    audit_action="primitive.filesystem.read_bytes.rejected",
                    context=effect_context,
                    error=error,
                    resource_source="primitive.filesystem.read_bytes",
                )
                raise error
            if target_state.kind != "file":
                error = CapabilityDenied(f"path is not a file: {relative}")
                self._complete_state_rejection(
                    protected,
                    pid=pid,
                    target=resource,
                    audit_action="primitive.filesystem.read_bytes.rejected",
                    context=effect_context,
                    error=error,
                    resource_source="primitive.filesystem.read_bytes",
                )
                raise error
            raw = protected.call(
                ProviderPhase("read", information_flow=True),
                self._provider_read_bytes,
                target,
                max_bytes=self._read_limit_for_state(target_state.size_bytes, max_bytes),
            )
            truncated = self._is_truncated_read(target_state.size_bytes, len(raw), max_bytes)
            selected = raw[:max_bytes]
            result = FileBytesReadResult(
                path=relative, content=selected, bytes_read=len(selected), truncated=truncated
            )
            result_payload = {"bytes_read": len(selected), "truncated": truncated}
            return protected.complete(
                result,
                self._protected_filesystem_evidence(
                    pid,
                    resource,
                    EventType.EXTERNAL_READ,
                    "primitive.filesystem.read_bytes",
                    {"adapter": "filesystem", "operation": "read_bytes", "path": relative, **result_payload},
                    {"path": relative, **result_payload},
                    result_payload,
                ),
                classification_context=effect_context,
                classification_result=result_payload,
                resource=ResourceSettlement(
                    usage=ResourceUsage(external_read_bytes=len(selected)),
                    source="primitive.filesystem.read_bytes",
                    context=effect_context,
                ),
            )

    def write_text(
        self,
        pid: str,
        path: str | os.PathLike[str],
        text: str,
        encoding: str = _TOOL_DEFAULTS.default_text_encoding,
        overwrite: bool = True,
        cwd: str | os.PathLike[str] | None = None,
    ) -> FileWriteResult:
        target, relative = self._resolve(path, cwd=cwd)
        resource = self.resource_for(relative)
        self._reject_definite_permission_denial(
            pid,
            resource,
            CapabilityRight.WRITE,
            context=self._authorization_context(
                pid=pid,
                resource=resource,
                relative=relative,
                primitive="runtime.filesystem.write_text",
                operation="write_text",
                right=CapabilityRight.WRITE.value,
                extra={"encoding": encoding, "overwrite": overwrite},
            ),
        )
        decision, authority_context = self._require_write(
            pid=pid,
            resource=resource,
            target=target,
            relative=relative,
            text=text,
            encoding=encoding,
            overwrite=overwrite,
        )
        bytes_to_write = len(text.encode(encoding))
        effect_context = {
            "path": relative,
            "resource": resource,
            "encoding": encoding,
            "overwrite": overwrite,
            "created": None,
        }
        intent: dict[str, Any] = {}

        def prepare() -> None:
            intent["record"] = self._record_mutation_intent(
                pid=pid,
                action="primitive.filesystem.write_text.intent",
                target=resource,
                decision={"path": relative, "bytes_to_write": bytes_to_write},
            )

        invocation = ProtectedOperationInvocation(
            pid=pid,
            actor=pid,
            target=resource,
            decisions=(decision,),
            canonical_args=authority_context,
            observation=effect_context,
            preflight_usage=ResourceUsage(external_write_bytes=bytes_to_write),
            resource_source="primitive.filesystem.write_text",
            resource_context=effect_context,
            prepare=prepare,
            failure_evidence=lambda error, phase: self._protected_failure_evidence(
                pid,
                resource,
                "primitive.filesystem.write_text.failed",
                effect_context,
                error,
                phase,
                intent.get("record"),
            ),
        )
        with self._protected().start("primitive.filesystem.write_text", invocation, provider=self.provider) as protected:
            target_state = protected.call(
                ProviderPhase("state", information_flow=True), self.provider.state, target
            )
            created = not target_state.exists
            effect_context.update({"created": created, "state_observed": True})
            if target_state.exists and target_state.kind != "file":
                error = CapabilityDenied(f"path is not a file: {relative}")
                self._complete_state_rejection(
                    protected,
                    pid=pid,
                    target=resource,
                    audit_action="primitive.filesystem.write_text.rejected",
                    context=effect_context,
                    error=error,
                    intent_record=intent.get("record"),
                    resource_source="primitive.filesystem.write_text",
                )
                raise error
            if target_state.exists and not overwrite:
                error = FileExistsError(f"file already exists: {relative}")
                self._complete_state_rejection(
                    protected,
                    pid=pid,
                    target=resource,
                    audit_action="primitive.filesystem.write_text.rejected",
                    context=effect_context,
                    error=error,
                    intent_record=intent.get("record"),
                    resource_source="primitive.filesystem.write_text",
                )
                raise error
            protected.call(
                ProviderPhase("write", state_mutation=True, information_flow=True),
                self.provider.write_text,
                target,
                text,
                encoding=encoding,
                newline="\n",
                overwrite=overwrite,
            )
            result = FileWriteResult(path=relative, bytes_written=bytes_to_write, created=created)
            result_payload = {"bytes_written": bytes_to_write, "created": created}
            return protected.complete(
                result,
                self._protected_filesystem_evidence(
                    pid,
                    resource,
                    EventType.EXTERNAL_WRITE,
                    "primitive.filesystem.write_text",
                    {"adapter": "filesystem", "path": relative, **result_payload},
                    {"path": relative, **result_payload},
                    result_payload,
                    intent.get("record"),
                ),
                classification_context=effect_context,
                classification_result=result_payload,
                resource=ResourceSettlement(
                    usage=ResourceUsage(external_write_bytes=bytes_to_write),
                    source="primitive.filesystem.write_text",
                    context=effect_context,
                ),
            )

    def read_directory(
        self,
        pid: str,
        path: str | os.PathLike[str],
        limit: int = _TOOL_DEFAULTS.directory_entry_limit,
        cwd: str | os.PathLike[str] | None = None,
    ) -> DirectoryReadResult:
        limit = self._bounded_positive_int(
            limit,
            label="limit",
            hard_limit=self.config.tools.directory_entry_hard_limit,
        )
        target, relative = self._resolve(path, cwd=cwd)
        resource = self.directory_resource_for(relative)
        authority_context = self._authorization_context(
            pid=pid,
            resource=resource,
            relative=relative,
            primitive="runtime.filesystem.read_directory",
            operation="read_directory",
            right=CapabilityRight.READ.value,
            extra={"limit": limit},
        )
        decision = self.capabilities.require(
            pid, resource, CapabilityRight.READ, authority_context, consume=False
        )
        effect_context = {"path": relative, "resource": resource, "limit": limit}
        estimated_metadata_bytes = self._directory_metadata_preflight_bytes(limit)
        invocation = ProtectedOperationInvocation(
            pid=pid,
            actor=pid,
            target=resource,
            decisions=(decision,),
            canonical_args=authority_context,
            observation=effect_context,
            preflight_usage=ResourceUsage(external_read_bytes=estimated_metadata_bytes),
            resource_source="primitive.filesystem.read_directory",
            resource_context={**effect_context, "estimated_metadata_bytes": estimated_metadata_bytes},
            failure_evidence=lambda error, phase: self._protected_failure_evidence(
                pid, resource, "primitive.filesystem.read_directory.failed", effect_context, error, phase
            ),
        )
        with self._protected().start("primitive.filesystem.read_directory", invocation, provider=self.provider) as protected:
            target_state = protected.call(
                ProviderPhase("state", information_flow=True), self.provider.state, target
            )
            if not target_state.exists:
                error = NotFound(f"directory does not exist: {relative}")
                self._complete_state_rejection(
                    protected,
                    pid=pid,
                    target=resource,
                    audit_action="primitive.filesystem.read_directory.rejected",
                    context=effect_context,
                    error=error,
                    resource_source="primitive.filesystem.read_directory",
                )
                raise error
            if target_state.kind != "directory":
                error = CapabilityDenied(f"path is not a directory: {relative}")
                self._complete_state_rejection(
                    protected,
                    pid=pid,
                    target=resource,
                    audit_action="primitive.filesystem.read_directory.rejected",
                    context=effect_context,
                    error=error,
                    resource_source="primitive.filesystem.read_directory",
                )
                raise error
            children = protected.call(
                ProviderPhase("list", information_flow=True),
                lambda: list(self.provider.list_directory(target, limit=limit + 1)),
            )
            selected = children[:limit]
            entries = [DirectoryEntry(**entry.__dict__) for entry in selected]
            truncated = len(children) > len(selected)
            metadata_bytes = self._directory_metadata_bytes(children)
            result = DirectoryReadResult(
                path=relative, entries=entries, count=len(entries), truncated=truncated
            )
            result_payload = {"count": len(entries), "truncated": truncated}
            return protected.complete(
                result,
                self._protected_filesystem_evidence(
                    pid,
                    resource,
                    EventType.EXTERNAL_READ,
                    "primitive.filesystem.read_directory",
                    {"adapter": "filesystem", "operation": "read_directory", "path": relative, **result_payload},
                    {"path": relative, **result_payload},
                    result_payload,
                ),
                classification_context=effect_context,
                classification_result=result_payload,
                resource=ResourceSettlement(
                    usage=ResourceUsage(external_read_bytes=metadata_bytes),
                    source="primitive.filesystem.read_directory",
                    context={**effect_context, "metadata_bytes": metadata_bytes, "listed_entries": len(children)},
                ),
            )

    def write_directory(
        self,
        pid: str,
        path: str | os.PathLike[str],
        parents: bool = True,
        exist_ok: bool = True,
        cwd: str | os.PathLike[str] | None = None,
    ) -> DirectoryWriteResult:
        target, relative = self._resolve(path, cwd=cwd)
        resource = self.directory_resource_for(relative)
        self._reject_definite_permission_denial(
            pid,
            resource,
            CapabilityRight.WRITE,
            context=self._authorization_context(
                pid=pid,
                resource=resource,
                relative=relative,
                primitive="runtime.filesystem.write_directory",
                operation="write_directory",
                right=CapabilityRight.WRITE.value,
                extra={"parents": parents, "exist_ok": exist_ok},
            ),
        )
        decision, authority_context = self._require_write_operation(
            pid=pid,
            resource=resource,
            target=target,
            relative=relative,
            operation="write_directory",
            primitive="runtime.filesystem.write_directory",
            question=f"Allow this process to create or update directory {relative}?",
            extra_context={"parents": parents, "exist_ok": exist_ok},
        )
        effect_context = {
            "path": relative,
            "resource": resource,
            "parents": parents,
            "exist_ok": exist_ok,
            "created": None,
        }
        intent: dict[str, Any] = {}

        def prepare() -> None:
            intent["record"] = self._record_mutation_intent(
                pid=pid,
                action="primitive.filesystem.write_directory.intent",
                target=resource,
                decision={"path": relative, "parents": parents, "exist_ok": exist_ok},
            )

        invocation = ProtectedOperationInvocation(
            pid=pid,
            actor=pid,
            target=resource,
            decisions=(decision,),
            canonical_args=authority_context,
            observation=effect_context,
            prepare=prepare,
            failure_evidence=lambda error, phase: self._protected_failure_evidence(
                pid,
                resource,
                "primitive.filesystem.write_directory.failed",
                effect_context,
                error,
                phase,
                intent.get("record"),
            ),
        )
        with self._protected().start(
            "primitive.filesystem.write_directory", invocation, provider=self.provider
        ) as protected:
            target_state = protected.call(
                ProviderPhase("state", information_flow=True), self.provider.state, target
            )
            created = not target_state.exists
            effect_context.update({"created": created, "state_observed": True})
            if target_state.exists and target_state.kind != "directory":
                error = CapabilityDenied(f"path is not a directory: {relative}")
                self._complete_state_rejection(
                    protected,
                    pid=pid,
                    target=resource,
                    audit_action="primitive.filesystem.write_directory.rejected",
                    context=effect_context,
                    error=error,
                    intent_record=intent.get("record"),
                )
                raise error
            if target_state.exists and not exist_ok:
                error = FileExistsError(f"directory already exists: {relative}")
                self._complete_state_rejection(
                    protected,
                    pid=pid,
                    target=resource,
                    audit_action="primitive.filesystem.write_directory.rejected",
                    context=effect_context,
                    error=error,
                    intent_record=intent.get("record"),
                )
                raise error
            protected.call(
                ProviderPhase("make_directory", state_mutation=True, information_flow=True),
                self.provider.make_directory,
                target,
                parents=parents,
                exist_ok=exist_ok,
            )
            result = DirectoryWriteResult(path=relative, created=created)
            result_payload = {"created": created}
            return protected.complete(
                result,
                self._protected_filesystem_evidence(
                    pid,
                    resource,
                    EventType.EXTERNAL_WRITE,
                    "primitive.filesystem.write_directory",
                    {"adapter": "filesystem", "operation": "write_directory", "path": relative, **result_payload},
                    {"path": relative, "parents": parents, "exist_ok": exist_ok, **result_payload},
                    result_payload,
                    intent.get("record"),
                ),
                classification_context=effect_context,
                classification_result=result_payload,
            )

    def delete_file(
        self,
        pid: str,
        path: str | os.PathLike[str],
        missing_ok: bool = False,
        cwd: str | os.PathLike[str] | None = None,
    ) -> DeleteResult:
        target, relative = self._resolve(path, cwd=cwd)
        resource = self.resource_for(relative)
        self._reject_definite_permission_denial(
            pid,
            resource,
            CapabilityRight.DELETE,
            context=self._authorization_context(
                pid=pid,
                resource=resource,
                relative=relative,
                primitive="runtime.filesystem.delete_file",
                operation="delete_file",
                right=CapabilityRight.DELETE.value,
                extra={"missing_ok": missing_ok},
            ),
        )
        decision, authority_context = self._require_delete(
            pid=pid,
            resource=resource,
            target=target,
            relative=relative,
            operation="delete_file",
            recursive=False,
            missing_ok=missing_ok,
        )
        effect_context = {"path": relative, "resource": resource, "missing_ok": missing_ok}
        intent: dict[str, Any] = {}

        def prepare() -> None:
            intent["record"] = self._record_mutation_intent(
                pid=pid,
                action="primitive.filesystem.delete_file.intent",
                target=resource,
                decision={"path": relative, "missing_ok": missing_ok},
            )

        invocation = ProtectedOperationInvocation(
            pid=pid,
            actor=pid,
            target=resource,
            decisions=(decision,),
            canonical_args=authority_context,
            observation=effect_context,
            prepare=prepare,
            failure_evidence=lambda error, phase: self._protected_failure_evidence(
                pid, resource, "primitive.filesystem.delete_file.failed", effect_context, error, phase, intent.get("record")
            ),
        )
        with self._protected().start("primitive.filesystem.delete_file", invocation, provider=self.provider) as protected:
            target_state = protected.call(
                ProviderPhase("state", information_flow=True), self.provider.state, target
            )
            effect_context["state_observed"] = True
            if not target_state.exists:
                if not missing_ok:
                    error = NotFound(f"file does not exist: {relative}")
                    self._complete_state_rejection(
                        protected,
                        pid=pid,
                        target=resource,
                        audit_action="primitive.filesystem.delete_file.rejected",
                        context=effect_context,
                        error=error,
                        intent_record=intent.get("record"),
                    )
                    raise error
                result = DeleteResult(path=relative, kind="missing", deleted=False)
                result_payload = {"path": relative, "deleted": False, "missing_ok": True}
                return protected.complete(
                    result,
                    self._protected_filesystem_evidence(
                        pid, resource, EventType.EXTERNAL_WRITE, "primitive.filesystem.delete_file",
                        {"adapter": "filesystem", "operation": "delete_file", **result_payload},
                        result_payload, result_payload, intent.get("record"),
                    ),
                    classification_override=self._state_only_classification(),
                )
            if target_state.kind != "file":
                error = CapabilityDenied(f"path is not a file: {relative}")
                self._complete_state_rejection(
                    protected,
                    pid=pid,
                    target=resource,
                    audit_action="primitive.filesystem.delete_file.rejected",
                    context=effect_context,
                    error=error,
                    intent_record=intent.get("record"),
                )
                raise error
            protected.call(
                ProviderPhase("delete", state_mutation=True, information_flow=True),
                self.provider.delete_file,
                target,
            )
            result = DeleteResult(path=relative, kind="file", deleted=True)
            result_payload = {"path": relative, "deleted": True}
            return protected.complete(
                result,
                self._protected_filesystem_evidence(
                    pid, resource, EventType.EXTERNAL_WRITE, "primitive.filesystem.delete_file",
                    {"adapter": "filesystem", "operation": "delete_file", "path": relative},
                    result_payload, {"deleted": True}, intent.get("record"),
                ),
                classification_context=effect_context,
                classification_result={"deleted": True},
            )

    def delete_directory(
        self,
        pid: str,
        path: str | os.PathLike[str],
        recursive: bool = False,
        missing_ok: bool = False,
        cwd: str | os.PathLike[str] | None = None,
    ) -> DeleteResult:
        target, relative = self._resolve(path, cwd=cwd)
        if target.is_root:
            raise CapabilityDenied("cannot delete filesystem adapter root")
        resource = self.directory_resource_for(relative)
        self._reject_definite_permission_denial(
            pid,
            resource,
            CapabilityRight.DELETE,
            context=self._authorization_context(
                pid=pid,
                resource=resource,
                relative=relative,
                primitive="runtime.filesystem.delete_directory",
                operation="delete_directory",
                right=CapabilityRight.DELETE.value,
                extra={"recursive": recursive, "missing_ok": missing_ok},
            ),
        )
        decision, authority_context = self._require_delete(
            pid=pid,
            resource=resource,
            target=target,
            relative=relative,
            operation="delete_directory",
            recursive=recursive,
            missing_ok=missing_ok,
        )
        effect_context = {
            "path": relative,
            "resource": resource,
            "recursive": recursive,
            "missing_ok": missing_ok,
        }
        intent: dict[str, Any] = {}

        def prepare() -> None:
            intent["record"] = self._record_mutation_intent(
                pid=pid,
                action="primitive.filesystem.delete_directory.intent",
                target=resource,
                decision={"path": relative, "recursive": recursive, "missing_ok": missing_ok},
            )

        invocation = ProtectedOperationInvocation(
            pid=pid,
            actor=pid,
            target=resource,
            decisions=(decision,),
            canonical_args=authority_context,
            observation=effect_context,
            prepare=prepare,
            failure_evidence=lambda error, phase: self._protected_failure_evidence(
                pid, resource, "primitive.filesystem.delete_directory.failed", effect_context, error, phase, intent.get("record")
            ),
        )
        with self._protected().start(
            "primitive.filesystem.delete_directory", invocation, provider=self.provider
        ) as protected:
            target_state = protected.call(
                ProviderPhase("state", information_flow=True), self.provider.state, target
            )
            effect_context["state_observed"] = True
            if not target_state.exists:
                if not missing_ok:
                    error = NotFound(f"directory does not exist: {relative}")
                    self._complete_state_rejection(
                        protected,
                        pid=pid,
                        target=resource,
                        audit_action="primitive.filesystem.delete_directory.rejected",
                        context=effect_context,
                        error=error,
                        intent_record=intent.get("record"),
                    )
                    raise error
                result = DeleteResult(
                    path=relative, kind="missing", deleted=False, recursive=recursive
                )
                result_payload = {
                    "path": relative,
                    "deleted": False,
                    "missing_ok": True,
                    "recursive": recursive,
                }
                return protected.complete(
                    result,
                    self._protected_filesystem_evidence(
                        pid, resource, EventType.EXTERNAL_WRITE, "primitive.filesystem.delete_directory",
                        {"adapter": "filesystem", "operation": "delete_directory", **result_payload},
                        result_payload, result_payload, intent.get("record"),
                    ),
                    classification_override=self._state_only_classification(),
                )
            if target_state.kind != "directory":
                error = CapabilityDenied(f"path is not a directory: {relative}")
                self._complete_state_rejection(
                    protected,
                    pid=pid,
                    target=resource,
                    audit_action="primitive.filesystem.delete_directory.rejected",
                    context=effect_context,
                    error=error,
                    intent_record=intent.get("record"),
                )
                raise error
            protected.call(
                ProviderPhase("delete", state_mutation=True, information_flow=True),
                self.provider.delete_directory,
                target,
                recursive=recursive,
            )
            result = DeleteResult(
                path=relative, kind="directory", deleted=True, recursive=recursive
            )
            result_payload = {"deleted": True, "recursive": recursive}
            return protected.complete(
                result,
                self._protected_filesystem_evidence(
                    pid, resource, EventType.EXTERNAL_WRITE, "primitive.filesystem.delete_directory",
                    {"adapter": "filesystem", "operation": "delete_directory", "path": relative, "recursive": recursive},
                    {"path": relative, **result_payload}, result_payload, intent.get("record"),
                ),
                classification_context=effect_context,
                classification_result=result_payload,
            )

    def grant_workspace(
        self,
        pid: str,
        rights: Iterable[str | CapabilityRight],
        issued_by: str = "filesystem",
        delegable: bool = True,
    ) -> Capability:
        return self.capabilities.grant(
            subject=pid,
            resource=self.workspace_resource(),
            rights=rights,
            issued_by=issued_by,
            delegable=delegable,
        )

    def grant_path(
        self,
        pid: str,
        path: str | os.PathLike[str],
        rights: Iterable[str | CapabilityRight],
        issued_by: str = "filesystem",
        cwd: str | os.PathLike[str] | None = None,
        delegable: bool = True,
    ) -> Capability:
        return self.capabilities.grant(
            subject=pid,
            resource=self.resource_for_path(path, cwd=cwd),
            rights=rights,
            issued_by=issued_by,
            delegable=delegable,
        )

    def grant_directory(
        self,
        pid: str,
        path: str | os.PathLike[str],
        rights: Iterable[str | CapabilityRight],
        issued_by: str = "filesystem",
        cwd: str | os.PathLike[str] | None = None,
        delegable: bool = True,
    ) -> Capability:
        return self.capabilities.grant(
            subject=pid,
            resource=self.directory_resource_for_path(path, cwd=cwd),
            rights=rights,
            issued_by=issued_by,
            delegable=delegable,
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
        cwd: str | os.PathLike[str] | None = None,
        delegable: bool = True,
    ) -> list[Capability]:
        grants: list[Capability] = []
        for path in read_files:
            grants.append(self.grant_path(pid, path, [CapabilityRight.READ], issued_by=issued_by, cwd=cwd, delegable=delegable))
        for path in write_files:
            grants.append(self.grant_path(pid, path, [CapabilityRight.WRITE], issued_by=issued_by, cwd=cwd, delegable=delegable))
        for path in delete_files:
            grants.append(self.grant_path(pid, path, [CapabilityRight.DELETE], issued_by=issued_by, cwd=cwd, delegable=delegable))
        for path in read_dirs:
            grants.append(self.grant_directory(pid, path, [CapabilityRight.READ], issued_by=issued_by, cwd=cwd, delegable=delegable))
        for path in write_dirs:
            grants.append(self.grant_directory(pid, path, [CapabilityRight.WRITE], issued_by=issued_by, cwd=cwd, delegable=delegable))
        for path in delete_dirs:
            grants.append(self.grant_directory(pid, path, [CapabilityRight.DELETE], issued_by=issued_by, cwd=cwd, delegable=delegable))
        return grants

    def workspace_resource(self) -> str:
        return f"filesystem:{self.namespace}:*"

    def resource_for(self, path: str | os.PathLike[str]) -> str:
        relative = self._resource_path(path)
        if relative in {"", "."}:
            return f"filesystem:{self.namespace}:"
        return f"filesystem:{self.namespace}:{relative}"

    def resource_for_path(self, path: str | os.PathLike[str], cwd: str | os.PathLike[str] | None = None) -> str:
        _target, relative = self._resolve(path, cwd=cwd)
        return self.resource_for(relative)

    def directory_resource_for(self, path: str | os.PathLike[str]) -> str:
        relative = self._resource_path(path).rstrip("/")
        if relative in {"", "."}:
            return self.workspace_resource()
        return f"filesystem:{self.namespace}:{relative}/*"

    def directory_resource_for_path(
        self,
        path: str | os.PathLike[str],
        cwd: str | os.PathLike[str] | None = None,
    ) -> str:
        _target, relative = self._resolve(path, cwd=cwd)
        return self.directory_resource_for(relative)

    def resolve_path(
        self,
        path: str | os.PathLike[str],
        cwd: str | os.PathLike[str] | None = None,
    ) -> tuple[ResolvedPath, str]:
        return self._resolve(path, cwd=cwd)

    def _protected(self) -> Any:
        sdk = (
            getattr(self, "protected_operations", None)
            or getattr(self, "protected_operation_sdk", None)
            or getattr(self.audit.store, "protected_operation_sdk", None)
        )
        if sdk is None:
            raise ValidationError("filesystem protected-operation SDK is not attached")
        return sdk

    @staticmethod
    def _state_only_classification(
        outcome: str = "state_observed",
    ) -> ExternalEffectClassification:
        return ExternalEffectClassification(
            rollback_class=ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED,
            rollback_status=ExternalEffectRollbackStatus.NOT_REQUIRED,
            state_mutation=False,
            information_flow=True,
            metadata={"outcome": outcome},
        )

    def _complete_state_rejection(
        self,
        protected: Any,
        *,
        pid: str,
        target: str,
        audit_action: str,
        context: dict[str, Any],
        error: BaseException,
        intent_record: Any | None = None,
        resource_source: str | None = None,
    ) -> None:
        outcome = "rejected_after_state_observation"
        result = {
            "outcome": outcome,
            "phase": "local_validation",
            "error_type": type(error).__name__,
        }
        resource = (
            ResourceSettlement(
                usage=ResourceUsage(),
                source=resource_source,
                context=context,
            )
            if resource_source is not None
            else None
        )
        protected.complete(
            None,
            self._protected_filesystem_evidence(
                pid,
                target,
                EventType.EXTERNAL_READ,
                audit_action,
                {"adapter": "filesystem", **result},
                {
                    "path": context.get("path"),
                    "effect_outcome": "failed",
                    **result,
                },
                result,
                intent_record,
            ),
            classification_override=self._state_only_classification(outcome),
            resource=resource,
        )

    def _protected_filesystem_evidence(
        self,
        pid: str,
        target: str,
        event_type: EventType,
        audit_action: str,
        event_payload: dict[str, Any],
        audit_decision: dict[str, Any],
        effect_metadata: dict[str, Any],
        intent_record: Any | None = None,
    ) -> ProtectedOperationEvidence:
        parent_id = getattr(intent_record, "record_id", None)
        return ProtectedOperationEvidence(
            event_type=event_type,
            event_source=pid,
            event_target=target,
            event_payload=event_payload,
            audit_action=audit_action,
            audit_actor=pid,
            audit_target=target,
            audit_decision=audit_decision,
            correlation_id=parent_id,
            parent_record_id=parent_id,
            effect_metadata=effect_metadata,
        )

    def _protected_failure_evidence(
        self,
        pid: str,
        target: str,
        audit_action: str,
        context: dict[str, Any],
        error: BaseException,
        phase: str,
        intent_record: Any | None = None,
    ) -> ProtectedOperationEvidence:
        is_mutation = any(
            marker in audit_action for marker in ("write", "delete", "make_directory")
        )
        return self._protected_filesystem_evidence(
            pid,
            target,
            EventType.EXTERNAL_WRITE if is_mutation else EventType.EXTERNAL_READ,
            audit_action,
            {
                "adapter": "filesystem",
                "outcome": "unknown",
                "phase": phase,
                "error_type": type(error).__name__,
            },
            {
                "path": context.get("path"),
                "effect_outcome": "unknown",
                "phase": phase,
                "error_type": type(error).__name__,
            },
            {"outcome": "unknown", "phase": phase, "error_type": type(error).__name__},
            intent_record,
        )

    def _read_limit_for_state(self, size_bytes: int | None, max_bytes: int) -> int:
        # The state snapshot is advisory: a file may grow between state() and
        # read_bytes(). If it already proves truncation, do not read beyond the
        # caller's information-flow budget. Otherwise request a sentinel byte
        # so growth cannot turn a partial read into a false complete result.
        if size_bytes is not None and size_bytes > max_bytes:
            return max_bytes
        return max_bytes + 1

    def _is_truncated_read(self, size_bytes: int | None, bytes_read: int, max_bytes: int) -> bool:
        return (size_bytes is not None and size_bytes > max_bytes) or bytes_read > max_bytes

    def _provider_read_bytes(self, target: ResolvedPath, *, max_bytes: int) -> bytes:
        try:
            return self.provider.read_bytes(target, max_bytes=max_bytes)
        except TypeError as exc:
            raise ValidationError("filesystem provider must support max_bytes-limited reads") from exc

    def _directory_metadata_bytes(self, entries: Iterable[Any]) -> int:
        payload = [getattr(entry, "__dict__", {"entry": str(entry)}) for entry in entries]
        return len(json.dumps(payload, ensure_ascii=True, default=str).encode("utf-8"))

    def _directory_metadata_preflight_bytes(self, limit: int) -> int:
        # Directory entry names and timestamps are only known after reading the
        # directory. Reserve a conservative per-entry envelope first so tight
        # information-flow budgets fail closed before metadata is observed.
        return max(1, (limit + 1) * 512)

    def _record_mutation_intent(
        self,
        *,
        pid: str,
        action: str,
        target: str,
        decision: dict[str, Any],
    ) -> Any:
        return self.audit.record(
            actor=pid,
            action=action,
            target=target,
            decision=decision,
        )

    def _resolve(
        self,
        path: str | os.PathLike[str],
        cwd: str | os.PathLike[str] | None = None,
    ) -> tuple[ResolvedPath, str]:
        target = self.provider.resolve(self._path_with_cwd(path, cwd))
        return target, target.relative

    def _logical_path(self, path: str | os.PathLike[str]) -> str:
        return os.fspath(path)

    def _resource_path(self, path: str | os.PathLike[str]) -> str:
        logical = self._logical_path(path)
        if logical in {"", "."}:
            return logical
        return "/".join(quote(part, safe=_RESOURCE_SEGMENT_SAFE) for part in logical.split("/"))

    def _path_with_cwd(
        self,
        path: str | os.PathLike[str],
        cwd: str | os.PathLike[str] | None,
    ) -> str:
        raw = os.fspath(path)
        if os.path.isabs(raw) or cwd is None or os.fspath(cwd) in {"", "."}:
            return raw
        cwd_path = self._logical_path(cwd).strip("/")
        if cwd_path in {"", "."}:
            return raw
        return f"{cwd_path}/{raw}"

    def _require_write(
        self,
        pid: str,
        resource: str,
        target: ResolvedPath,
        relative: str,
        text: str,
        encoding: str,
        overwrite: bool,
    ) -> tuple[CapabilityDecision, dict[str, Any]]:
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
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        # Do not stat the target before a definite deny/miss; existence and
        # kind are filesystem facts that require some matching policy first.
        policy = self.capabilities.permission_policy(pid, resource, right, context)
        if policy in {CapabilityManager.MISSING, CapabilityManager.ALWAYS_DENY}:
            self.capabilities.require(pid, resource, right, context)

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
    ) -> tuple[CapabilityDecision, dict[str, Any]]:
        operation_context = self._operation_context(
            pid=pid,
            resource=resource,
            target=target,
            relative=relative,
            primitive=primitive,
            operation=operation,
            right=CapabilityRight.WRITE.value,
            extra=extra_context or {},
        )
        decision = self.capabilities.authorize(pid, resource, CapabilityRight.WRITE, operation_context)
        if decision.allowed:
            return decision, operation_context
        if decision.policy == CapabilityManager.ALWAYS_DENY:
            raise CapabilityDenied(f"{pid} denied write on {resource}")
        if decision.policy == CapabilityManager.ASK_EACH_TIME:
            if self.human is None:
                raise CapabilityDenied(f"{pid} requires human approval for write on {resource}")
            # This primitive has the concrete path, caller-declared overwrite
            # policy, byte count, and preview needed for a safe per-use human
            # decision. Target state is deliberately deferred until after the
            # one-time approval has been issued and reserved.
            request_id = self.human.query(
                pid=pid,
                    human=self.config.runtime.default_human,
                request={
                    "type": "external_operation_approval",
                    "question": question,
                    "requested_once_capability": {
                        "subject": pid,
                        "resource": resource,
                        "rights": [CapabilityRight.WRITE.value],
                        "constraints": self._approval_constraints(operation_context, right=CapabilityRight.WRITE.value),
                    },
                    "context": {
                        **operation_context,
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
    ) -> tuple[CapabilityDecision, dict[str, Any]]:
        operation_context = self._operation_context(
            pid=pid,
            resource=resource,
            target=target,
            relative=relative,
            primitive=f"runtime.filesystem.{operation}",
            operation=operation,
            right=CapabilityRight.DELETE.value,
            extra={"recursive": recursive, "missing_ok": missing_ok},
        )
        decision = self.capabilities.authorize(pid, resource, CapabilityRight.DELETE, operation_context)
        if decision.allowed:
            return decision, operation_context
        if decision.policy == CapabilityManager.ALWAYS_DENY:
            raise CapabilityDenied(f"{pid} denied delete on {resource}")
        if decision.policy == CapabilityManager.ASK_EACH_TIME:
            if self.human is None:
                raise CapabilityDenied(f"{pid} requires human approval for delete on {resource}")
            request_id = self.human.query(
                pid=pid,
                    human=self.config.runtime.default_human,
                request={
                    "type": "external_operation_approval",
                    "question": f"Allow this process to delete {relative}?",
                    "requested_once_capability": {
                        "subject": pid,
                        "resource": resource,
                        "rights": [CapabilityRight.DELETE.value],
                        "constraints": self._approval_constraints(operation_context, right=CapabilityRight.DELETE.value),
                    },
                    "context": operation_context,
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
        profile = self.capabilities.profiles.filesystem(
            resource=resource,
            right=right,
            effect=CapabilityEffect.ASK,
            risk=self._risk_for_filesystem_right(right),
            path=relative,
        )
        return {
            "adapter": "filesystem",
            "primitive": primitive,
            "operation": operation,
            "authority_operation": f"filesystem.{right}",
            "pid": pid,
            "workspace_root": self.root,
            "path": relative,
            "absolute_path": target.display,
            "resource": resource,
            "right": right,
            "sandbox_profile": self._profile_json(profile),
            "grant_scope": "one_time",
            "target_state_observation": "deferred_until_authorized",
            **extra,
        }

    def _authorization_context(
        self,
        *,
        pid: str,
        resource: str,
        relative: str,
        primitive: str,
        operation: str,
        right: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        profile = self.capabilities.profiles.filesystem(
            resource=resource,
            right=right,
            effect=CapabilityEffect.ALLOW,
            risk=self._risk_for_filesystem_right(right),
            path=relative,
        )
        return {
            "adapter": "filesystem",
            "primitive": primitive,
            "operation": operation,
            "authority_operation": f"filesystem.{right}",
            "pid": pid,
            "workspace_root": self.root,
            "path": relative,
            "resource": resource,
            "right": right,
            "sandbox_profile": self._profile_json(profile),
            **(extra or {}),
        }

    def _approval_constraints(self, context: dict[str, Any], *, right: str) -> dict[str, Any]:
        condition_keys = [
            "path",
            "content_sha256",
            "overwrite",
            "parents",
            "exist_ok",
            "recursive",
            "missing_ok",
        ]
        conditions = {key: context[key] for key in condition_keys if key in context}
        return {
            AUTHORITY_RULES_KEY: [
                {
                    "rule_id": f"filesystem.approval.{right}",
                    "operation": f"filesystem.{right}",
                    "effect": CapabilityEffect.ALLOW.value,
                    "risk": self._risk_for_filesystem_right(right).value,
                    "conditions": conditions,
                    "description": "one-shot human approval for exact filesystem operation",
                }
            ]
        }

    def _profile_json(self, profile: Any) -> dict[str, Any]:
        return {
            "operation": profile.operation,
            "resource": profile.resource,
            "effect": profile.effect.value,
            "risk": profile.risk.value,
            "rule_id": profile.rule_id,
            "restrictions": profile.restrictions,
        }

    def _risk_for_filesystem_right(self, right: str) -> AuthorityRisk:
        if right == CapabilityRight.DELETE.value:
            return AuthorityRisk.DESTRUCTIVE
        if right == CapabilityRight.WRITE.value:
            return AuthorityRisk.HIGH
        return AuthorityRisk.LOW

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

    def _preview_text(self, text: str, limit: int | None = None) -> tuple[str, bool]:
        selected_limit = self.config.tools.approval_preview_chars if limit is None else limit
        preview = text[:selected_limit]
        # repr() prevents newlines or prompt-like text from masquerading as
        # separate approval instructions in the human terminal prompt.
        return repr(preview), len(text) > selected_limit

    def _decode_text_prefix(self, data: bytes, encoding: str, *, truncated: bool) -> str:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as exc:
            if truncated and exc.end == len(data):
                return data[: exc.start].decode(encoding)
            raise

    def _bounded_positive_int(self, value: int, *, label: str, hard_limit: int) -> int:
        if isinstance(value, bool):
            raise ValidationError(f"{label} must be an integer")
        try:
            selected = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{label} must be an integer") from exc
        if selected < 1:
            raise ValidationError(f"{label} must be >= 1")
        if selected > hard_limit:
            raise ValidationError(f"{label} exceeds hard limit {hard_limit}")
        return selected

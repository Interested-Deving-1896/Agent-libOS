from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.models.base import StrEnum


class OperationKind(StrEnum):
    LLM_REQUEST = "llm_request"
    TOOL_CALL = "tool_call"
    SYSCALL = "syscall"
    PRIMITIVE = "primitive"
    RUNTIME = "runtime"


class OperationState(StrEnum):
    RUNNING = "running"
    WAITING = "waiting"
    TERMINAL = "terminal"


class OperationOutcome(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    DENIED = "denied"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class OperationEvidenceRole(StrEnum):
    INVOCATION = "invocation"
    DECISION = "decision"
    RESERVATION = "reservation"
    APPROVAL = "approval"
    EFFECT = "effect"
    RESOURCE_CHARGE = "resource_charge"
    RESULT = "result"
    CONTEXT = "context"
    EVENT = "event"
    AUDIT = "audit"
    WAIT = "wait"


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    root_operation_id: str
    parent_operation_id: str | None
    kind: OperationKind
    name: str
    actor: str
    pid: str | None
    state: OperationState
    outcome: OperationOutcome
    expected_roles: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None


@dataclass(frozen=True)
class OperationEvidenceLink:
    link_id: str
    operation_id: str
    evidence_type: str
    evidence_id: str
    role: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextMaterializationManifest:
    materialization_id: str
    pid: str
    view_id: str
    policy: str
    budget_tokens: int
    rendered_tokens: int
    rendered_sha256: str
    context_generation: str | None
    context_oid: str | None
    context_version: int | None
    objects: list[dict[str, Any]] = field(default_factory=list)
    compaction: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

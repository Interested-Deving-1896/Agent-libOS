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


@dataclass(frozen=True, order=True, slots=True)
class OperationCursor:
    """Stable keyset cursor for bounded startup operation recovery."""

    started_at: str
    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.started_at, str) or not self.started_at:
            raise ValueError("operation cursor started_at must not be empty")
        if not isinstance(self.operation_id, str) or not self.operation_id:
            raise ValueError("operation cursor operation_id must not be empty")


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


@dataclass(frozen=True, slots=True)
class OperationPage:
    """One hard-bounded page of stale running operations."""

    records: tuple[OperationRecord, ...]
    next_cursor: OperationCursor | None = None

    def __post_init__(self) -> None:
        if self.next_cursor is not None and not self.records:
            raise ValueError("empty operation page cannot have a cursor")


@dataclass(frozen=True, slots=True)
class StaleOperationRecoverySummary:
    """Bounded diagnostics for a fully processed stale-operation backlog."""

    total_count: int
    sample_operation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.total_count, bool)
            or not isinstance(self.total_count, int)
            or self.total_count < 0
        ):
            raise ValueError("stale operation recovery total must be non-negative")
        if len(self.sample_operation_ids) > self.total_count:
            raise ValueError("stale operation recovery sample exceeds total")

    @property
    def truncated(self) -> bool:
        return len(self.sample_operation_ids) < self.total_count

    def __len__(self) -> int:
        return self.total_count


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

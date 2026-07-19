from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from agent_libos.models.base import CapabilityID, PID, ToolID, StrEnum
from agent_libos.models.memory import ObjectHandle

JIT_MULTIPLEXER_TOOL_NAME = "run_jit_tool"
OPENAI_TOOL_NAME_MAX_CHARS = 64
OPENAI_TOOL_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
_OPENAI_TOOL_NAME_RE = re.compile(OPENAI_TOOL_NAME_PATTERN)


def is_openai_tool_name(value: str) -> bool:
    return bool(_OPENAI_TOOL_NAME_RE.fullmatch(value))


class ToolCandidateStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    REJECTED = "rejected"
    REGISTERED = "registered"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    version: str = "1.0.0"
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    required_capabilities: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)


@dataclass
class ToolCandidate:
    candidate_id: str
    pid: PID
    spec: ToolSpec
    source_code: str
    tests: list[dict[str, Any]]
    requested_capabilities: list[dict[str, Any]]
    status: ToolCandidateStatus
    validation: dict[str, Any] | None
    created_at: str
    updated_at: str
    registered_tool_id: ToolID | None = None


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    logs: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolHandle:
    tool_id: ToolID
    name: str
    capability_id: CapabilityID | None
    scope: str = "ephemeral_process"


@dataclass(frozen=True, slots=True)
class JITRehydrationArtifact:
    """Exact durable JIT rows needed to validate one process binding."""

    tool_id: ToolID
    name: str
    scope: str
    candidate_match_count: int
    candidate_id: str | None = None
    candidate_pid: PID | None = None
    source_code: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("tool_id", "name", "scope"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"JIT rehydration artifact {field_name} must be non-empty")
        if (
            isinstance(self.candidate_match_count, bool)
            or not isinstance(self.candidate_match_count, int)
            or self.candidate_match_count < 0
        ):
            raise ValueError("JIT rehydration candidate count must be non-negative")
        candidate_values = (self.candidate_id, self.candidate_pid, self.source_code)
        if self.candidate_match_count == 1:
            if (
                not isinstance(self.candidate_id, str)
                or not self.candidate_id
                or not isinstance(self.candidate_pid, str)
                or not self.candidate_pid
                or not isinstance(self.source_code, str)
            ):
                raise ValueError(
                    "exact JIT rehydration candidate metadata is incomplete"
                )
        elif any(value is not None for value in candidate_values):
            raise ValueError(
                "ambiguous JIT rehydration candidates cannot expose candidate metadata"
            )

    @property
    def rehydratable(self) -> bool:
        return self.candidate_match_count == 1 and bool(self.source_code)


@dataclass(frozen=True, slots=True)
class JITRehydrationRecord:
    """Bounded diagnostic identity for one restored or pruned JIT binding."""

    pid: PID
    tool_id: ToolID
    name: str

    def __post_init__(self) -> None:
        for field_name in ("pid", "tool_id", "name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"JIT rehydration record {field_name} must be non-empty")

    def to_mapping(self) -> dict[str, str]:
        return {"pid": self.pid, "tool_id": self.tool_id, "name": self.name}


@dataclass(frozen=True, slots=True)
class JITRehydrationSummary:
    """Exact totals with page-bounded startup recovery diagnostics."""

    restored_total: int
    pruned_stale_total: int
    restored_sample: tuple[JITRehydrationRecord, ...] = ()
    pruned_stale_sample: tuple[JITRehydrationRecord, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("restored_total", "pruned_stale_total"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"JIT rehydration {field_name} must be non-negative")
        if len(self.restored_sample) > self.restored_total:
            raise ValueError("JIT rehydration restored sample exceeds total")
        if len(self.pruned_stale_sample) > self.pruned_stale_total:
            raise ValueError("JIT rehydration pruned sample exceeds total")

    @property
    def restored_truncated(self) -> bool:
        return len(self.restored_sample) < self.restored_total

    @property
    def pruned_stale_truncated(self) -> bool:
        return len(self.pruned_stale_sample) < self.pruned_stale_total


@dataclass(frozen=True)
class ToolCallResult:
    call_id: str
    tool_id: ToolID
    result_handle: ObjectHandle | None
    payload: Any
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class WorkflowRunResult:
    pid: PID
    image: str
    tool: str
    ok: bool
    status: str
    call_id: str | None = None
    tool_id: ToolID | None = None
    result_oid: str | None = None
    payload: Any = None
    error: str | None = None
    waiting_human: bool = False
    request_id: str | None = None
    waiting_process: bool = False
    child_pid: PID | None = None
    waiting_message: bool = False
    filters: dict[str, Any] | None = None

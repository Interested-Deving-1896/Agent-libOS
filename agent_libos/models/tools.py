from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.models.base import CapabilityID, PID, ToolID, StrEnum
from agent_libos.models.memory import ObjectHandle


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


@dataclass(frozen=True)
class ToolCallResult:
    call_id: str
    tool_id: ToolID
    result_handle: ObjectHandle | None
    payload: Any
    ok: bool
    error: str | None = None

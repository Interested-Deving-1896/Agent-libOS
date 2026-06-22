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

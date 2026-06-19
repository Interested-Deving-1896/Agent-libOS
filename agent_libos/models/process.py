from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models.base import CapabilityID, CheckpointID, EventID, OID, PID, StrEnum
from agent_libos.models.memory import MemoryView, ObjectHandle

_PROCESS_DEFAULTS = DEFAULT_CONFIG.process


class ProcessStatus(StrEnum):
    CREATED = "created"
    RUNNABLE = "runnable"
    RUNNING = "running"
    WAITING_EVENT = "waiting_event"
    WAITING_TOOL = "waiting_tool"
    WAITING_HUMAN = "waiting_human"
    PAUSED = "paused"
    SUSPENDED = "suspended"
    EXITED = "exited"
    FAILED = "failed"
    KILLED = "killed"


class ForkMode(StrEnum):
    COPY = "copy"
    RESTRICTED = "restricted"
    SPECULATIVE = "speculative"
    WORKER = "worker"


class ProcessSignal(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    INTERRUPT = "interrupt"
    TERMINATE = "terminate"


@dataclass
class ResourceBudget:
    max_tool_calls: int | None = _PROCESS_DEFAULTS.max_tool_calls
    max_child_processes: int | None = _PROCESS_DEFAULTS.max_child_processes
    max_runtime_seconds: int | None = _PROCESS_DEFAULTS.max_runtime_seconds
    max_materialized_tokens: int = _PROCESS_DEFAULTS.max_materialized_tokens
    max_llm_calls: int | None = _PROCESS_DEFAULTS.max_llm_calls
    max_llm_total_tokens: int | None = _PROCESS_DEFAULTS.max_llm_total_tokens
    max_subprocess_wall_seconds: float | None = _PROCESS_DEFAULTS.max_subprocess_wall_seconds
    max_subprocess_cpu_seconds: float | None = _PROCESS_DEFAULTS.max_subprocess_cpu_seconds
    max_subprocess_memory_bytes: int | None = _PROCESS_DEFAULTS.max_subprocess_memory_bytes
    max_external_read_bytes: int | None = _PROCESS_DEFAULTS.max_external_read_bytes
    max_external_write_bytes: int | None = _PROCESS_DEFAULTS.max_external_write_bytes
    max_jsonrpc_bytes: int | None = _PROCESS_DEFAULTS.max_jsonrpc_bytes
    max_deno_syscalls: int | None = _PROCESS_DEFAULTS.max_deno_syscalls

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            allow_none = item.name != "max_materialized_tokens"
            _validate_resource_number(item.name, value, allow_none=allow_none)


@dataclass
class ResourceUsage:
    runtime_seconds: float = 0.0
    tool_calls: int = 0
    child_processes: int = 0
    llm_calls: int = 0
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    llm_total_tokens: int = 0
    subprocess_wall_seconds: float = 0.0
    subprocess_cpu_seconds: float = 0.0
    subprocess_peak_memory_bytes: int = 0
    external_read_bytes: int = 0
    external_write_bytes: int = 0
    jsonrpc_request_bytes: int = 0
    jsonrpc_response_bytes: int = 0
    deno_syscalls: int = 0

    def __post_init__(self) -> None:
        for item in fields(self):
            _validate_resource_number(item.name, getattr(self, item.name), allow_none=False)


def _validate_resource_number(name: str, value: Any, *, allow_none: bool) -> None:
    if value is None and allow_none:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    if value < 0:
        raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
class AgentImage:
    image_id: str
    name: str
    version: str = "v0"
    system_prompt: str = ""
    planner: dict[str, Any] = field(default_factory=dict)
    action_schema: dict[str, Any] = field(default_factory=dict)
    default_skills: list[str] = field(default_factory=list)
    default_tools: list[str] = field(default_factory=list)
    context_policy: str = "plan_first"
    safety_profile: str = "default"
    required_capabilities: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    signature: str | None = None
    boot: dict[str, Any] = field(default_factory=lambda: {"kind": "fresh"})


@dataclass
class AgentProcess:
    pid: PID
    parent_pid: PID | None
    image_id: str
    status: ProcessStatus
    goal_oid: OID | None
    memory_view: MemoryView | None
    capabilities: list[CapabilityID]
    loaded_skills: dict[str, Any]
    tool_table: dict[str, str]
    event_cursor: EventID | None
    checkpoint_head: CheckpointID | None
    resource_budget: ResourceBudget
    resource_usage: ResourceUsage
    created_at: str
    updated_at: str
    working_directory: str = "."
    status_message: str | None = None


@dataclass
class ProcessResult:
    pid: PID
    status: ProcessStatus
    result: ObjectHandle | None = None
    message: str | None = None

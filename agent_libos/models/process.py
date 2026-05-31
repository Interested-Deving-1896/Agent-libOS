from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.models.base import CapabilityID, CheckpointID, EventID, OID, PID, StrEnum
from agent_libos.models.memory import MemoryView, ObjectHandle


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
    max_tool_calls: int = 256
    max_child_processes: int = 16
    max_runtime_seconds: int | None = None
    max_materialized_tokens: int = 65536


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


@dataclass
class AgentProcess:
    pid: PID
    parent_pid: PID | None
    image_id: str
    status: ProcessStatus
    goal_oid: OID | None
    memory_view: MemoryView | None
    capabilities: list[CapabilityID]
    loaded_skills: dict[str, str]
    tool_table: dict[str, str]
    event_cursor: EventID | None
    checkpoint_head: CheckpointID | None
    resource_budget: ResourceBudget
    created_at: str
    updated_at: str
    status_message: str | None = None


@dataclass
class ProcessResult:
    pid: PID
    status: ProcessStatus
    result: ObjectHandle | None = None
    message: str | None = None

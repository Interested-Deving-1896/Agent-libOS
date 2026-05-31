from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

PID = str
OID = str
CapabilityID = str
EventID = str
AuditID = str
CheckpointID = str
ToolID = str
HumanRequestID = str
MemoryViewID = str
SnapshotID = str


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ObjectType(StrEnum):
    TASK = "task"
    GOAL = "goal"
    PLAN = "plan"
    STEP = "step"
    CONSTRAINT = "constraint"
    MESSAGE = "message"
    HUMAN_DECISION = "human_decision"
    HUMAN_REQUEST = "human_request"
    TOOL_RESULT = "tool_result"
    OBSERVATION = "observation"
    ERROR_TRACE = "error_trace"
    CODE_PATCH = "code_patch"
    TEST_RESULT = "test_result"
    EVIDENCE = "evidence"
    CLAIM = "claim"
    SUMMARY = "summary"
    SKILL = "skill"
    TOOL_SPEC = "tool_spec"
    TOOL_CANDIDATE = "tool_candidate"
    TOOL_ARTIFACT = "tool_artifact"
    CHECKPOINT = "checkpoint"
    PROCESS_STATE = "process_state"
    EXTERNAL_REF = "external_ref"
    ARTIFACT = "artifact"


class ObjectRight(StrEnum):
    READ = "read"
    WRITE = "write"
    LINK = "link"
    DIFF = "diff"
    MATERIALIZE = "materialize"
    DELETE = "delete"
    GRANT = "grant"


class CapabilityRight(StrEnum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    LINK = "link"
    DIFF = "diff"
    MATERIALIZE = "materialize"
    DELETE = "delete"
    GRANT = "grant"
    REVOKE = "revoke"
    APPROVE = "approve"
    ADMIN = "admin"


class RelationType(StrEnum):
    HAS_PLAN = "has_plan"
    HAS_STEP = "has_step"
    CONSTRAINED_BY = "constrained_by"
    SUPPORTED_BY = "supported_by"
    PRODUCED = "produced"
    EVALUATED_BY = "evaluated_by"
    DERIVED_FROM = "derived_from"
    SUMMARIZES = "summarizes"
    REFERENCES = "references"
    APPROVED_BY = "approved_by"
    REJECTED_BY = "rejected_by"
    SUPERSEDES = "supersedes"
    BLOCKED_BY = "blocked_by"
    ASSIGNED_TO = "assigned_to"


class ViewMode(StrEnum):
    READ_ONLY = "read_only"
    COPY_ON_WRITE = "copy_on_write"
    MUTABLE = "mutable"
    EPHEMERAL = "ephemeral"


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


class EventType(StrEnum):
    PROCESS_CREATED = "process_created"
    PROCESS_FORKED = "process_forked"
    PROCESS_EXITED = "process_exited"
    PROCESS_SIGNAL = "process_signal"
    OBJECT_CREATED = "object_created"
    OBJECT_UPDATED = "object_updated"
    OBJECT_LINKED = "object_linked"
    HUMAN_QUERY = "human_query"
    HUMAN_RESPONSE = "human_response"
    TOOL_CALLED = "tool_called"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    CAPABILITY_GRANTED = "capability_granted"
    CAPABILITY_REVOKED = "capability_revoked"
    CHECKPOINT_CREATED = "checkpoint_created"
    ROLLBACK = "rollback"
    EXTERNAL_READ = "external_read"
    EXTERNAL_WRITE = "external_write"
    HUMAN_OUTPUT = "human_output"


class EventPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class HumanRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"
    CANCELLED = "cancelled"
    DELIVERED = "delivered"


class ToolCandidateStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    REJECTED = "rejected"
    REGISTERED = "registered"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_HUMAN_APPROVAL = "require_human_approval"
    REQUIRE_SANDBOX = "require_sandbox"
    REQUIRE_CHECKPOINT = "require_checkpoint"
    REQUIRE_CAPABILITY_ATTENUATION = "require_capability_attenuation"


@dataclass
class ObjectMetadata:
    title: str | None = None
    summary: str | None = None
    tags: list[str] = field(default_factory=list)
    mime_type: str | None = None
    token_estimate: int | None = None
    embedding_refs: list[str] = field(default_factory=list)
    indexes: list[str] = field(default_factory=list)
    sensitivity: str = "normal"
    retention_policy: str = "default"


@dataclass
class Provenance:
    source_refs: list[str] = field(default_factory=list)
    created_from_action: str | None = None
    parent_oids: list[OID] = field(default_factory=list)


@dataclass(frozen=True)
class AgentObject:
    oid: OID
    name: str
    type: ObjectType
    schema_version: str
    payload: Any
    metadata: ObjectMetadata
    provenance: Provenance
    version: int
    immutable: bool
    created_by: PID | str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ObjectHandle:
    oid: OID
    rights: set[str]
    capability_id: CapabilityID
    expires_at: str | None = None


@dataclass
class ObjectFilter:
    type: ObjectType | None = None
    tags: list[str] = field(default_factory=list)
    text: str | None = None


@dataclass
class ObjectQuery:
    type: ObjectType | str | None = None
    tags: list[str] = field(default_factory=list)
    text: str | None = None
    limit: int = 50
    name: str | None = None


@dataclass
class ObjectPatch:
    name: str | None = None
    payload: Any | None = None
    metadata: ObjectMetadata | None = None
    provenance: Provenance | None = None


@dataclass(frozen=True)
class ObjectLink:
    link_id: str
    src: OID
    relation: RelationType
    dst: OID
    metadata: dict[str, Any]
    created_by: PID | str
    created_at: str


@dataclass
class MemoryView:
    view_id: MemoryViewID
    owner_pid: PID
    roots: list[ObjectHandle]
    filters: list[ObjectFilter]
    rights_policy: str
    created_from: MemoryViewID | SnapshotID | None
    mode: ViewMode


@dataclass
class MemoryViewSpec:
    roots: list[ObjectHandle] | None = None
    mode: ViewMode = ViewMode.READ_ONLY
    include_parent_roots: bool = True
    rights: set[str] | None = None


@dataclass
class MergePolicy:
    include_child_created: bool = True
    include_updated: bool = True
    grant_rights: set[str] = field(default_factory=lambda: {"read", "materialize", "link"})


@dataclass
class MergeResult:
    merged_oids: list[OID]
    skipped_oids: list[OID]


@dataclass
class MaterializedContext:
    text: str
    object_refs: list[OID]
    token_count: int
    omitted_objects: list[OID]
    policy_used: str


@dataclass(frozen=True)
class Capability:
    cap_id: CapabilityID
    subject: str
    resource: str
    rights: set[str]
    constraints: dict[str, Any]
    issued_by: str
    issued_at: str
    expires_at: str | None = None
    delegable: bool = False
    revocable: bool = True
    revoked: bool = False


@dataclass(frozen=True)
class Event:
    event_id: EventID
    type: EventType
    source: str
    target: str | None
    payload: dict[str, Any]
    priority: EventPriority
    created_at: str
    correlation_id: str | None = None
    causality: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditRecord:
    record_id: AuditID
    timestamp: str
    actor: str
    action: str
    target: str | None
    input_refs: list[OID]
    output_refs: list[OID]
    capability_refs: list[CapabilityID]
    decision: dict[str, Any] | None
    correlation_id: str | None
    parent_record_id: AuditID | None = None


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


@dataclass
class HumanRequest:
    request_id: HumanRequestID
    pid: PID
    human: str
    payload: dict[str, Any]
    status: HumanRequestStatus
    decision: dict[str, Any] | None
    blocking: bool
    created_at: str
    updated_at: str


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
class Checkpoint:
    checkpoint_id: CheckpointID
    pid: PID
    reason: str
    created_at: str

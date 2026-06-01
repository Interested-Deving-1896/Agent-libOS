from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models.base import CapabilityID, MemoryViewID, NamespaceID, OID, PID, SnapshotID, StrEnum

_MEMORY_DEFAULTS = DEFAULT_CONFIG.memory


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


@dataclass
class ObjectMetadata:
    title: str | None = None
    summary: str | None = None
    tags: list[str] = field(default_factory=list)
    mime_type: str | None = None
    token_estimate: int | None = None
    embedding_refs: list[str] = field(default_factory=list)
    indexes: list[str] = field(default_factory=list)
    sensitivity: str = _MEMORY_DEFAULTS.metadata_sensitivity
    retention_policy: str = _MEMORY_DEFAULTS.metadata_retention_policy


@dataclass
class Provenance:
    source_refs: list[str] = field(default_factory=list)
    created_from_action: str | None = None
    parent_oids: list[OID] = field(default_factory=list)


@dataclass(frozen=True)
class ObjectNamespace:
    namespace: NamespaceID
    parent_namespace: NamespaceID | None
    metadata: dict[str, Any]
    created_by: PID | str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AgentObject:
    oid: OID
    namespace: NamespaceID
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
    limit: int = _MEMORY_DEFAULTS.query_limit
    name: str | None = None
    namespace: NamespaceID | None = None


@dataclass
class ObjectPatch:
    name: str | None = None
    namespace: NamespaceID | None = None
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

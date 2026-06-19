from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.models.base import CapabilityID, StrEnum


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


class CapabilityEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class CapabilityStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    DISABLED = "disabled"


class AuthorityRisk(StrEnum):
    HARMLESS = "harmless"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    DESTRUCTIVE = "destructive"


class ResourceScope(StrEnum):
    EXACT = "exact"
    SUBTREE = "subtree"
    PREFIX = "prefix"
    GLOBAL = "global"


@dataclass(frozen=True)
class ResourcePattern:
    """Canonical typed resource pattern used for capability matching."""

    raw: str
    kind: str
    body: str
    scope: ResourceScope


@dataclass(frozen=True)
class OperationContext:
    """Primitive-specific authorization context recorded with decisions."""

    primitive: str | None = None
    operation: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthorityRule:
    """Deterministic rule attached to an authority grant or runtime profile."""

    rule_id: str
    operation: str
    effect: CapabilityEffect
    risk: AuthorityRisk
    conditions: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class CapabilityLease:
    expires_at: str | None = None
    uses_remaining: int | None = None


@dataclass(frozen=True)
class DelegationPolicy:
    delegable: bool = False
    revocable: bool = True
    max_delegation_depth: int | None = None


@dataclass(frozen=True)
class SandboxProfile:
    operation: str
    resource: str
    effect: CapabilityEffect
    risk: AuthorityRisk
    rule_id: str | None = None
    restrictions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilitySpec:
    resource: str
    rights: set[str]
    effect: CapabilityEffect = CapabilityEffect.ALLOW
    rules: list[AuthorityRule | dict[str, Any]] = field(default_factory=list)
    lease: CapabilityLease | dict[str, Any] | None = None
    delegation: DelegationPolicy | dict[str, Any] | None = None
    constraints: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    expires_at: str | None = None
    uses_remaining: int | None = None
    delegable: bool = False
    revocable: bool = True
    max_delegation_depth: int | None = None


@dataclass(frozen=True)
class CapabilityDecision:
    subject: str
    resource: str
    right: str
    allowed: bool
    effect: CapabilityEffect | None
    reason: str
    matched_capability_ids: list[CapabilityID] = field(default_factory=list)
    selected_capability_id: CapabilityID | None = None
    consume_capability_id: CapabilityID | None = None
    human_request_id: str | None = None
    issuer_chain: list[CapabilityID] = field(default_factory=list)
    constraint_results: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def policy(self) -> str:
        if self.effect == CapabilityEffect.ALLOW:
            return "allow_once" if self.consume_capability_id else "always_allow"
        if self.effect == CapabilityEffect.DENY:
            return "always_deny"
        if self.effect == CapabilityEffect.ASK:
            return "ask_each_time"
        return "missing"


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
    effect: CapabilityEffect = CapabilityEffect.ALLOW
    issuer_cap_id: CapabilityID | None = None
    parent_cap_id: CapabilityID | None = None
    delegation_depth: int = 0
    max_delegation_depth: int | None = None
    uses_remaining: int | None = None
    status: CapabilityStatus = CapabilityStatus.ACTIVE
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def revoked(self) -> bool:
        return self.status == CapabilityStatus.REVOKED

    @property
    def active(self) -> bool:
        return self.status == CapabilityStatus.ACTIVE

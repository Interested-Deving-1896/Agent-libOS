from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.models.base import AuditID, EventID, PID, StrEnum


class ExternalEffectRollbackClass(StrEnum):
    IRREVERSIBLE = "irreversible"
    ROLLBACKABLE = "rollbackable"
    NO_ROLLBACK_REQUIRED = "no_rollback_required"
    UNKNOWN = "unknown"


class ExternalEffectRollbackStatus(StrEnum):
    NOT_SUPPORTED = "not_supported"
    NOT_APPLIED = "not_applied"
    NOT_REQUIRED = "not_required"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExternalEffectClassification:
    rollback_class: ExternalEffectRollbackClass
    rollback_status: ExternalEffectRollbackStatus
    state_mutation: bool
    information_flow: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalEffectRecord:
    effect_id: str
    record_id: AuditID | None
    event_id: EventID | None
    pid: PID
    provider: str
    operation: str
    target: str | None
    rollback_class: ExternalEffectRollbackClass
    rollback_status: ExternalEffectRollbackStatus
    state_mutation: bool
    information_flow: bool
    provider_metadata: dict[str, Any]
    created_at: str

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_libos.models.base import AuditID, CapabilityID, OID


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

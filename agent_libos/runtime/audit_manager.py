from __future__ import annotations

from typing import Any

from agent_libos.ids import new_id, utc_now
from agent_libos.models import AuditRecord
from agent_libos.storage import SQLiteStore


class AuditManager:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def record(
        self,
        actor: str,
        action: str,
        target: str | None = None,
        input_refs: list[str] | None = None,
        output_refs: list[str] | None = None,
        capability_refs: list[str] | None = None,
        decision: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        parent_record_id: str | None = None,
    ) -> AuditRecord:
        record = AuditRecord(
            record_id=new_id("audit"),
            timestamp=utc_now(),
            actor=actor,
            action=action,
            target=target,
            input_refs=input_refs or [],
            output_refs=output_refs or [],
            capability_refs=capability_refs or [],
            decision=decision,
            correlation_id=correlation_id,
            parent_record_id=parent_record_id,
        )
        self.store.insert_audit(record)
        return record

    def trace(self, limit: int | None = None) -> list[AuditRecord]:
        return self.store.list_audit(limit=limit)


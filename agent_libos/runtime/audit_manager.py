from __future__ import annotations

from typing import Any

from agent_libos.utils.ids import new_id, utc_now
from agent_libos.models import AuditRecord
from agent_libos.storage import RuntimeStore


class AuditManager:
    def __init__(self, store: RuntimeStore):
        self.store = store
        self.operations: Any | None = None

    def bind_operations(self, operations: Any) -> None:
        self.operations = operations

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
        if self.operations is not None:
            self.operations.link_evidence("audit", record.record_id, "audit")
            semantic_role = self._semantic_role(action)
            if semantic_role is not None:
                self.operations.link_evidence("audit", record.record_id, semantic_role)
        return record

    @staticmethod
    def _semantic_role(action: str) -> str | None:
        if action == "capability.authorize":
            return "decision"
        if action.startswith("capability.") and any(
            marker in action for marker in ("reserve", "consume", "restore")
        ):
            return "reservation"
        if action.startswith("human.") and any(
            marker in action for marker in ("approve", "reject", "response", "terminal")
        ):
            return "approval"
        if action == "resource.charge":
            return "resource_charge"
        if action in {"tool.call", "syscall.result"}:
            return "result"
        return None

    def trace(
        self,
        limit: int | None = None,
        *,
        actor: str | None = None,
        target: str | None = None,
        match_any: bool = False,
    ) -> list[AuditRecord]:
        return self.store.list_audit(limit=limit, actor=actor, target=target, match_any=match_any)

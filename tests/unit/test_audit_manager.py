from __future__ import annotations

from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.storage import SQLiteStore


class TestAuditManager:
    def test_trace_limit_returns_latest_records_in_chronological_order(self) -> None:
        store = SQLiteStore(":memory:")
        audit = AuditManager(store)

        for index in range(5):
            audit.record(actor="pid_test", action=f"audit.{index}", target="process:pid_test")

        records = audit.trace(limit=2)

        assert [record.action for record in records] == ["audit.3", "audit.4"]

    def test_trace_filters_before_applying_limit(self) -> None:
        store = SQLiteStore(":memory:")
        audit = AuditManager(store)
        audit.record(actor="pid_target", action="target.first", target="process:pid_target")
        for index in range(5):
            audit.record(actor="pid_noise", action=f"noise.{index}", target="process:pid_noise")

        records = audit.trace(limit=1, actor="pid_target", target="process:pid_target", match_any=True)

        assert [record.action for record in records] == ["target.first"]

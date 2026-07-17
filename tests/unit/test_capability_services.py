from __future__ import annotations

from pathlib import Path

import pytest

from agent_libos.capability import (
    CapabilityEvaluator,
    CapabilityLeaseService,
    CapabilityManager,
    CapabilityMutationService,
)
from agent_libos.capability.effect_binding import APPROVAL_BINDING_KEY, canonical_effect_hash
from agent_libos.models import Capability, CapabilityEffect, CapabilityRight
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.runtime.operation_manager import OperationManager
from agent_libos.storage import SQLiteStore


def _capability(
    cap_id: str,
    *,
    effect: CapabilityEffect,
    constraints: dict | None = None,
    uses_remaining: int | None = None,
) -> Capability:
    return Capability(
        cap_id=cap_id,
        subject="worker",
        resource="object:record",
        rights={CapabilityRight.READ.value},
        constraints=dict(constraints or {}),
        issued_by="test",
        issued_at="2026-01-01T00:00:00+00:00",
        effect=effect,
        uses_remaining=uses_remaining,
    )


def _manager(*, audit=None):
    store = SQLiteStore(":memory:")
    selected_audit = audit or AuditManager(store)
    events = EventBus(store)
    operations = OperationManager(store)
    manager = CapabilityManager(
        store,
        selected_audit,
        events,
        operations=operations,
    )
    return store, manager


def test_evaluator_is_side_effect_free_and_preserves_deny_precedence() -> None:
    evaluator = CapabilityEvaluator()
    allow = _capability("cap_allow", effect=CapabilityEffect.ALLOW, uses_remaining=1)
    deny = _capability("cap_deny", effect=CapabilityEffect.DENY)

    decision = evaluator.decide(
        subject="worker",
        resource="object:record",
        requested_right=CapabilityRight.READ.value,
        matches=evaluator.sort_matching_capabilities([allow, deny]),
        issuer_chains={allow.cap_id: [allow.cap_id], deny.cap_id: [deny.cap_id]},
    )

    assert not decision.allowed
    assert decision.effect == CapabilityEffect.DENY
    assert decision.selected_capability_id == deny.cap_id
    assert decision.consume_capability_id is None


def test_evaluator_uses_neutral_effect_binding_without_runtime_dependency() -> None:
    evaluator = CapabilityEvaluator()
    context = {"operation": "update", "record_id": "7", "value": "approved"}
    cap = _capability(
        "cap_approval",
        effect=CapabilityEffect.ALLOW,
        constraints={
            APPROVAL_BINDING_KEY: {
                "effect_id": "eff_test",
                "canonical_args_hash": canonical_effect_hash(context),
                "target_state_version": None,
            }
        },
        uses_remaining=1,
    )

    allowed = evaluator.decide(
        subject=cap.subject,
        resource=cap.resource,
        requested_right=CapabilityRight.READ.value,
        matches=[cap],
        context=context,
    )
    changed = evaluator.decide(
        subject=cap.subject,
        resource=cap.resource,
        requested_right=CapabilityRight.READ.value,
        matches=[cap],
        context={**context, "value": "changed"},
    )

    assert allowed.allowed and allowed.consume_capability_id == cap.cap_id
    assert not changed.allowed
    assert changed.effect is None


def test_manager_is_a_compatible_facade_over_three_services() -> None:
    store, manager = _manager()
    try:
        assert isinstance(manager.evaluator, CapabilityEvaluator)
        assert isinstance(manager.leases, CapabilityLeaseService)
        assert isinstance(manager.mutations, CapabilityMutationService)

        cap = manager.issue_trusted(
            "worker",
            "object:record",
            [CapabilityRight.READ],
            issued_by="test",
            uses_remaining=1,
        )
        decision = manager.require(
            "worker",
            cap.resource,
            CapabilityRight.READ,
            consume=False,
        )
        reservation_id = manager.reserve_decision_use(
            decision,
            used_by="test",
            reason="service boundary test",
        )

        assert reservation_id is not None
        assert store.get_capability(cap.cap_id).uses_remaining == 0
        restored = manager.restore_reserved_use(reservation_id, restored_by="test")
        assert restored is not None and restored.uses_remaining == 1
    finally:
        store.close()


def test_issue_rolls_back_when_mutation_audit_sink_fails() -> None:
    store = SQLiteStore(":memory:")
    delegate = AuditManager(store)

    class FailingIssueAudit:
        def record(self, *args, **kwargs):
            if kwargs.get("action") == "capability.issue":
                raise RuntimeError("injected issue audit failure")
            return delegate.record(*args, **kwargs)

    manager = CapabilityManager(
        store,
        FailingIssueAudit(),
        EventBus(store),
        operations=OperationManager(store),
    )
    try:
        with pytest.raises(RuntimeError, match="issue audit failure"):
            manager.issue_trusted(
                "worker",
                "object:atomic",
                [CapabilityRight.READ],
                issued_by="test",
            )

        assert store.list_capabilities(subject="worker") == []
    finally:
        store.close()


def test_capability_core_has_no_runtime_implementation_imports() -> None:
    package = Path(__file__).parents[2] / "agent_libos" / "capability"
    for source in package.glob("*.py"):
        assert "from agent_libos.runtime" not in source.read_text(encoding="utf-8"), source.name

from __future__ import annotations

import pytest

from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.runtime.lifecycle import RuntimeLifecycle
from agent_libos.runtime.operation_manager import OperationManager
from agent_libos.capability.manager import CapabilityManager
from agent_libos.storage import SQLiteStore


def test_internal_recovery_lease_does_not_escape_its_builder_scope() -> None:
    store = SQLiteStore(":memory:")
    operations = OperationManager(store)
    audit = AuditManager(store, operations)
    events = EventBus(store, operations)
    lifecycle = RuntimeLifecycle(
        store=store,
        audit=audit,
        events=events,
        substrate=object(),
    )
    lifecycle.begin_recovery()
    capabilities = CapabilityManager(
        store,
        audit,
        events,
        operations=operations,
        admission=lifecycle,
    )
    try:
        with lifecycle.recovery_lease():
            internal = capabilities.issue_trusted(
                "runtime.recovery",
                "custom:internal-recovery",
                ["read"],
                issued_by="test",
            )

        with pytest.raises(RuntimeError, match="state=recovering"):
            capabilities.issue_trusted(
                "external-caller",
                "custom:ordinary-mutation",
                ["read"],
                issued_by="test",
            )

        assert store.get_capability(internal.cap_id) is not None
        assert store.list_capabilities("external-caller") == []
    finally:
        store.close()


def test_recovery_lease_requires_the_lifecycle_opaque_identity() -> None:
    store = SQLiteStore(":memory:")
    operations = OperationManager(store)
    audit = AuditManager(store, operations)
    events = EventBus(store, operations)
    lifecycle = RuntimeLifecycle(
        store=store,
        audit=audit,
        events=events,
        substrate=object(),
    )
    lifecycle.begin_recovery()
    try:
        with pytest.raises(RuntimeError, match="active startup recovery lease"):
            lifecycle.require_recovery_lease()

        forged = lifecycle._internal_admission.set(object())
        try:
            with pytest.raises(RuntimeError, match="active startup recovery lease"):
                lifecycle.require_recovery_lease()
        finally:
            lifecycle._internal_admission.reset(forged)

        with lifecycle.recovery_lease():
            lifecycle.require_recovery_lease()

        with pytest.raises(RuntimeError, match="active startup recovery lease"):
            lifecycle.require_recovery_lease()
    finally:
        store.close()

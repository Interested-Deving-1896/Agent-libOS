from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from agent_libos import Runtime
from agent_libos.models import (
    CapabilityRight,
    EventType,
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    HumanRequest,
    HumanRequestStatus,
    ResourceUsage,
)
from agent_libos.models.exceptions import ValidationError
from agent_libos.sdk import (
    PostProviderFailureMode,
    ProtectedOperationContract,
    ProtectedOperationEvidence,
    ProtectedOperationInvocation,
    ProtectedOperationProtocolError,
    ProviderPhase,
    ResourcePolicy,
    ResourceSettlement,
)
from agent_libos.substrate import ProviderEffectNotStarted
from agent_libos.utils.ids import new_id, utc_now
from tests.support.runtime import temporary_runtime


class _Provider:
    def classify_external_effect(self, _operation, _context, _result):
        return ExternalEffectClassification(
            rollback_class=ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED,
            rollback_status=ExternalEffectRollbackStatus.NOT_REQUIRED,
            state_mutation=False,
            information_flow=True,
            metadata={"provider_receipt": {"id": "receipt-1"}},
        )


def _evidence(pid: str) -> ProtectedOperationEvidence:
    return ProtectedOperationEvidence(
        event_type=EventType.EXTERNAL_READ,
        event_source=pid,
        event_target="test:item",
        event_payload={"ok": True},
        audit_action="primitive.test.read",
        audit_actor=pid,
        audit_target="test:item",
        audit_decision={"ok": True},
        effect_metadata={"ok": True},
        provider_receipt={"id": "receipt-1"},
    )


def _setup(runtime, *, preserve_result: bool = False):
    pid = runtime.process.spawn(goal="protected operation sdk")
    capability = runtime.capability.issue_trusted(
        pid,
        "test:item",
        [CapabilityRight.READ],
        issued_by="test",
        uses_remaining=1,
    )
    decision = runtime.capability.require(
        pid,
        "test:item",
        CapabilityRight.READ,
        consume=False,
    )
    contract = ProtectedOperationContract(
        name="primitive.test.read",
        provider="test",
        operation="read",
        evidence_roles=("audit", "event", "effect"),
        resource_policy=ResourcePolicy.NONE,
        information_flow=True,
        post_provider_failure_mode=(
            PostProviderFailureMode.PRESERVE_RESULT
            if preserve_result
            else PostProviderFailureMode.PROPAGATE
        ),
    )
    runtime.protected_operations.register_contract(contract)
    invocation = ProtectedOperationInvocation(
        pid=pid,
        actor=pid,
        target="test:item",
        decisions=(decision,),
        canonical_args={"item": "secret-value"},
        observation={"item_sha256": "safe-hash"},
    )
    return pid, capability, contract, invocation


def test_sdk_success_consumes_authority_and_persists_safe_evidence() -> None:
    with temporary_runtime() as runtime:
        pid, capability, contract, invocation = _setup(runtime)
        provider = _Provider()

        with runtime.protected_operations.start(contract, invocation, provider=provider) as operation:
            result = operation.call(
                ProviderPhase("read", information_flow=True),
                lambda: {"value": "provider-secret"},
            )
            returned = operation.complete(
                result,
                _evidence(pid),
                classification_result={"ok": True},
            )

        assert returned == {"value": "provider-secret"}
        assert runtime.store.get_capability(capability.cap_id).uses_remaining == 0
        effect = runtime.store.list_external_effects(pid=pid)[0]
        assert effect.effect_state == "finalized"
        assert effect.transaction_state == "committed"
        serialized = str(effect.provider_metadata)
        assert "secret-value" not in serialized
        assert "provider-secret" not in serialized
        assert effect.canonical_args_hash
        assert effect.provider_metadata["provider_phases"] == [
            {"name": "read", "state_mutation": False, "information_flow": True}
        ]


def test_sdk_preserves_classifier_provider_receipt_when_evidence_has_none() -> None:
    with temporary_runtime() as runtime:
        pid, _capability, contract, invocation = _setup(runtime)
        with runtime.protected_operations.start(contract, invocation, provider=_Provider()) as operation:
            result = operation.call(ProviderPhase("read", information_flow=True), lambda: "ok")
            operation.complete(result, replace(_evidence(pid), provider_receipt={}))
        effect = runtime.store.list_external_effects(pid=pid)[0]
        assert effect.provider_receipt == {"id": "receipt-1"}


def test_sdk_first_not_started_restores_reservation_and_abandons_intent() -> None:
    with temporary_runtime() as runtime:
        _pid, capability, contract, invocation = _setup(runtime)
        provider = _Provider()

        with pytest.raises(ProviderEffectNotStarted):
            with runtime.protected_operations.start(contract, invocation, provider=provider) as operation:
                operation.call(
                    ProviderPhase("read", information_flow=True),
                    lambda: (_ for _ in ()).throw(ProviderEffectNotStarted("not started")),
                )

        assert runtime.store.get_capability(capability.cap_id).uses_remaining == 1
        assert runtime.store.list_external_effects(pid=invocation.pid) == []
        operation = next(
            item
            for item in runtime.store.list_operations(pid=invocation.pid)
            if item.name == contract.name
        )
        explanation = runtime.explain.explain_operation(operation.operation_id)
        assert explanation["evidence_complete"] is True
        assert explanation["missing_evidence"] == []


def test_sdk_later_not_started_keeps_prior_information_flow() -> None:
    with temporary_runtime() as runtime:
        pid, capability, contract, invocation = _setup(runtime)
        provider = _Provider()

        with pytest.raises(ProviderEffectNotStarted):
            with runtime.protected_operations.start(contract, invocation, provider=provider) as operation:
                operation.call(ProviderPhase("metadata", information_flow=True), lambda: "observed")
                operation.call(
                    ProviderPhase("body", information_flow=True),
                    lambda: (_ for _ in ()).throw(ProviderEffectNotStarted("body not started")),
                )

        assert runtime.store.get_capability(capability.cap_id).uses_remaining == 0
        effect = runtime.store.list_external_effects(pid=pid)[0]
        assert effect.effect_state == "finalized"
        assert effect.transaction_state == "committed"
        assert effect.information_flow is True
        assert effect.provider_metadata["outcome"] == "partial_not_started_after_prior_provider_effect"
        assert effect.provider_metadata["provider_phases"][0]["name"] == "metadata"


def test_sdk_ordinary_provider_failure_is_unknown() -> None:
    with temporary_runtime() as runtime:
        pid, capability, contract, invocation = _setup(runtime)
        provider = _Provider()

        with pytest.raises(RuntimeError, match="ambiguous"):
            with runtime.protected_operations.start(contract, invocation, provider=provider) as operation:
                operation.call(
                    ProviderPhase("read", information_flow=True),
                    lambda: (_ for _ in ()).throw(RuntimeError("ambiguous secret text")),
                )

        assert runtime.store.get_capability(capability.cap_id).uses_remaining == 0
        effect = runtime.store.list_external_effects(pid=pid)[0]
        assert effect.transaction_state == "unknown"
        assert "ambiguous secret text" not in str(effect.provider_metadata)


def test_sdk_required_resource_policy_charges_preflight_on_provider_failure() -> None:
    with temporary_runtime() as runtime:
        pid, _capability, _contract, invocation = _setup(runtime)
        contract = ProtectedOperationContract(
            name="primitive.test.metered_failure",
            provider="test",
            operation="read",
            evidence_roles=("audit", "event", "effect"),
            resource_policy=ResourcePolicy.REQUIRED,
            information_flow=True,
        )
        runtime.protected_operations.register_contract(contract)
        metered = ProtectedOperationInvocation(
            **{
                **invocation.__dict__,
                "preflight_usage": ResourceUsage(external_read_bytes=10),
                "resource_source": "test.metered_failure",
            }
        )

        with pytest.raises(RuntimeError, match="ambiguous"):
            with runtime.protected_operations.start(
                contract, metered, provider=_Provider()
            ) as operation:
                operation.call(
                    ProviderPhase("read", information_flow=True),
                    lambda: (_ for _ in ()).throw(RuntimeError("ambiguous")),
                )

        assert runtime.process.get(pid).resource_usage.external_read_bytes == 10
        effect = runtime.store.list_external_effects(pid=pid)[0]
        assert effect.transaction_state == "unknown"
        assert any(
            record.action == "resource.charge"
            and record.decision.get("source") == "test.metered_failure"
            for record in runtime.audit.trace()
        )


def test_sdk_preserve_result_does_not_replay_after_settlement_failure(monkeypatch) -> None:
    with temporary_runtime() as runtime:
        pid, _capability, contract, invocation = _setup(runtime, preserve_result=True)
        provider = _Provider()
        calls = 0
        emit = runtime.events.emit

        def provider_call():
            nonlocal calls
            calls += 1
            return "delivered"

        with runtime.protected_operations.start(contract, invocation, provider=provider) as operation:
            result = operation.call(ProviderPhase("read", information_flow=True), provider_call)
            monkeypatch.setattr(
                runtime.events,
                "emit",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("event sink failed")),
            )
            assert operation.complete(result, _evidence(pid)) == "delivered"
            monkeypatch.setattr(runtime.events, "emit", emit)

        assert calls == 1
        effect = runtime.store.list_external_effects(pid=pid)[0]
        assert effect.effect_state == "pending"


def test_sdk_async_phase_and_missing_complete_fail_closed() -> None:
    async def run() -> None:
        with temporary_runtime() as runtime:
            _pid, _capability, contract, invocation = _setup(runtime)
            provider = _Provider()

            async def provider_call() -> str:
                await asyncio.sleep(0)
                return "ok"

            with pytest.raises(ProtectedOperationProtocolError, match="without complete"):
                with runtime.protected_operations.start(contract, invocation, provider=provider) as operation:
                    assert await operation.acall(
                        ProviderPhase("read", information_flow=True),
                        provider_call,
                    ) == "ok"

            effect = runtime.store.list_external_effects(pid=invocation.pid)[0]
            assert effect.transaction_state == "unknown"

    asyncio.run(run())


def test_sdk_prepare_failure_rolls_back_all_reservations_and_intent() -> None:
    with temporary_runtime() as runtime:
        pid, capability, contract, invocation = _setup(runtime)
        failing = ProtectedOperationInvocation(
            **{
                **invocation.__dict__,
                "prepare": lambda: (_ for _ in ()).throw(RuntimeError("prepare failed")),
            }
        )

        with pytest.raises(RuntimeError, match="prepare failed"):
            with runtime.protected_operations.start(contract, failing, provider=_Provider()):
                pass

        assert runtime.store.get_capability(capability.cap_id).uses_remaining == 1
        assert runtime.store.list_external_effects(pid=pid) == []


def test_sdk_duplicate_decision_is_reserved_once() -> None:
    with temporary_runtime() as runtime:
        pid, capability, contract, invocation = _setup(runtime)
        duplicate = ProtectedOperationInvocation(
            **{**invocation.__dict__, "decisions": (invocation.decisions[0],) * 2}
        )
        with runtime.protected_operations.start(contract, duplicate, provider=_Provider()) as operation:
            result = operation.call(ProviderPhase("read", information_flow=True), lambda: "ok")
            operation.complete(result, _evidence(pid))
        assert runtime.store.get_capability(capability.cap_id).uses_remaining == 0


def test_sdk_context_without_provider_phase_fails_closed_and_abandons() -> None:
    with temporary_runtime() as runtime:
        pid, capability, contract, invocation = _setup(runtime)
        with pytest.raises(ProtectedOperationProtocolError, match="without provider phase"):
            with runtime.protected_operations.start(contract, invocation, provider=_Provider()):
                pass
        assert runtime.store.get_capability(capability.cap_id).uses_remaining == 1
        assert runtime.store.list_external_effects(pid=pid) == []


def test_sdk_dispatch_cas_failure_restores_without_calling_provider(monkeypatch) -> None:
    with temporary_runtime() as runtime:
        pid, capability, contract, invocation = _setup(runtime)
        calls = 0

        def provider_call() -> str:
            nonlocal calls
            calls += 1
            return "unexpected"

        monkeypatch.setattr(runtime.store, "transition_external_effect", lambda *_args, **_kwargs: False)
        with pytest.raises(ValidationError, match="cannot be dispatched"):
            with runtime.protected_operations.start(contract, invocation, provider=_Provider()) as operation:
                operation.call(ProviderPhase("read", information_flow=True), provider_call)

        assert calls == 0
        assert runtime.store.get_capability(capability.cap_id).uses_remaining == 1
        assert runtime.store.list_external_effects(pid=pid) == []
        operation_record = next(
            item for item in runtime.store.list_operations(pid=pid) if item.name == contract.name
        )
        assert runtime.explain.explain_operation(operation_record.operation_id)["evidence_complete"] is True


def test_sdk_classifier_failure_uses_contract_ceiling_without_error_text() -> None:
    class FailingClassifier(_Provider):
        def classify_external_effect(self, _operation, _context, _result):
            raise RuntimeError("classifier-secret")

    with temporary_runtime() as runtime:
        pid, _capability, contract, invocation = _setup(runtime)
        with runtime.protected_operations.start(contract, invocation, provider=FailingClassifier()) as operation:
            result = operation.call(ProviderPhase("read", information_flow=True), lambda: "ok")
            operation.complete(result, _evidence(pid))
        effect = runtime.store.list_external_effects(pid=pid)[0]
        assert effect.information_flow is True
        assert effect.provider_metadata["classification_error_type"] == "RuntimeError"
        assert "classifier-secret" not in str(effect.provider_metadata)


def test_sdk_event_failure_does_not_retry_provider_and_keeps_pending(monkeypatch) -> None:
    with temporary_runtime() as runtime:
        pid, _capability, contract, invocation = _setup(runtime)
        calls = 0
        original_emit = runtime.events.emit
        with pytest.raises(RuntimeError, match="event failed"):
            with runtime.protected_operations.start(contract, invocation, provider=_Provider()) as operation:
                def provider_call() -> str:
                    nonlocal calls
                    calls += 1
                    return "ok"

                result = operation.call(ProviderPhase("read", information_flow=True), provider_call)
                monkeypatch.setattr(
                    runtime.events,
                    "emit",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("event failed")),
                )
                operation.complete(result, _evidence(pid))
        monkeypatch.setattr(runtime.events, "emit", original_emit)
        assert calls == 1
        effect = runtime.store.list_external_effects(pid=pid)[0]
        assert effect.effect_state == "pending"
        assert effect.transaction_state == "dispatched"


def test_sdk_resource_charge_failure_happens_after_effect_commit(monkeypatch) -> None:
    with temporary_runtime() as runtime:
        pid, _capability, _contract, invocation = _setup(runtime)
        contract = ProtectedOperationContract(
            name="primitive.test.resource_read",
            provider="test",
            operation="read",
            evidence_roles=("audit", "event", "effect"),
            resource_policy=ResourcePolicy.REQUIRED,
            information_flow=True,
        )
        runtime.protected_operations.register_contract(contract)
        metered = ProtectedOperationInvocation(
            **{
                **invocation.__dict__,
                "preflight_usage": ResourceUsage(external_read_bytes=1),
                "resource_source": "test",
            }
        )
        monkeypatch.setattr(
            runtime.resources,
            "charge",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("charge failed")),
        )
        with pytest.raises(RuntimeError, match="charge failed"):
            with runtime.protected_operations.start(contract, metered, provider=_Provider()) as operation:
                result = operation.call(ProviderPhase("read", information_flow=True), lambda: "ok")
                operation.complete(
                    result,
                    _evidence(pid),
                    resource=ResourceSettlement(
                        ResourceUsage(external_read_bytes=1),
                        source="test",
                    ),
                )
        effect = runtime.store.list_external_effects(pid=pid)[0]
        assert effect.effect_state == "finalized"
        assert effect.transaction_state == "committed"


def test_sdk_async_cancellation_consumes_authority_and_records_unknown() -> None:
    async def run() -> None:
        with temporary_runtime() as runtime:
            pid, capability, contract, invocation = _setup(runtime)

            async def cancelled() -> str:
                raise asyncio.CancelledError

            with pytest.raises(asyncio.CancelledError):
                with runtime.protected_operations.start(contract, invocation, provider=_Provider()) as operation:
                    await operation.acall(ProviderPhase("read", information_flow=True), cancelled)
            assert runtime.store.get_capability(capability.cap_id).uses_remaining == 0
            effect = runtime.store.list_external_effects(pid=pid)[0]
            assert effect.transaction_state == "unknown"

    asyncio.run(run())


def test_sdk_cleanup_failure_does_not_leak_operation_scope() -> None:
    with temporary_runtime() as runtime:
        _pid, _capability, contract, invocation = _setup(runtime)
        failing_restore = ProtectedOperationInvocation(
            **{
                **invocation.__dict__,
                "restore_not_started": lambda: (_ for _ in ()).throw(
                    RuntimeError("restore failed")
                ),
            }
        )

        handle = runtime.protected_operations.start(
            contract, failing_restore, provider=_Provider()
        )
        with pytest.raises(RuntimeError, match="restore failed"):
            with handle:
                pass

        assert runtime.operations.current() is None


def test_sdk_recovers_prepared_reservation_without_provider_reconciliation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "prepared-recovery.sqlite"
    runtime = Runtime.open(database)
    operation = None
    try:
        _pid, capability, contract, invocation = _setup(runtime)
        operation = runtime.protected_operations.start(
            contract, invocation, provider=_Provider()
        )
        operation.__enter__()
        effect_id = operation.effect_id
        assert effect_id is not None
        assert runtime.store.get_external_effect(effect_id).transaction_state == "prepared"
        assert runtime.store.get_capability(capability.cap_id).uses_remaining == 0

        # Simulate process loss after the atomic prepare transaction but before
        # the first provider phase. Close only the explain scope; do not run the
        # protected-operation exit protocol that would normally restore it.
        scope = operation._operation_cm
        assert scope is not None
        interrupted = RuntimeError("simulated runtime crash")
        scope.__exit__(type(interrupted), interrupted, interrupted.__traceback__)
        operation._operation_cm = None
        runtime.store.close()
        runtime._closed = True

        reopened = Runtime.open(database)
        try:
            restored = reopened.store.get_capability(capability.cap_id)
            assert restored is not None
            assert restored.uses_remaining == 1
            assert restored.active
            assert reopened.store.get_external_effect(effect_id) is None
            reservation = reopened.store.get_capability_use_reservation(
                operation._reservation_ids[0]
            )
            assert reservation is not None
            assert reservation["status"] == "restored"
        finally:
            reopened.close()
    finally:
        if not runtime._closed:
            runtime.close()


def test_sdk_recovery_restores_prepared_human_output_claim(tmp_path: Path) -> None:
    database = tmp_path / "prepared-human.sqlite"
    runtime = Runtime.open(database)
    try:
        pid = runtime.process.spawn(goal="prepared human recovery")
        now = utc_now()
        request = HumanRequest(
            request_id=new_id("hreq"),
            pid=pid,
            human="owner",
            payload={"type": "output", "message": "secret", "channel": "terminal"},
            status=HumanRequestStatus.PENDING,
            decision=None,
            blocking=False,
            created_at=now,
            updated_at=now,
        )
        runtime.store.insert_human_request(request)

        def prepare() -> None:
            current = runtime.store.get_human_request(request.request_id)
            assert current is not None
            current.status = HumanRequestStatus.DELIVERED
            current.decision = {"delivery_committed": True}
            current.updated_at = utc_now()
            runtime.store.update_human_request(current)

        invocation = ProtectedOperationInvocation(
            pid=pid,
            actor=pid,
            target="human:owner",
            canonical_args={"request_id": request.request_id, "message": "secret"},
            observation={
                "request_id": request.request_id,
                "request_kind": "output",
                "chars": 6,
            },
            prepare=prepare,
        )
        operation = runtime.protected_operations.start(
            "primitive.human.write",
            invocation,
            provider=runtime.human.provider,
        )
        operation.__enter__()
        effect_id = operation.effect_id
        assert effect_id is not None
        assert runtime.store.get_human_request(request.request_id).status == HumanRequestStatus.DELIVERED

        scope = operation._operation_cm
        assert scope is not None
        interrupted = RuntimeError("simulated runtime crash")
        scope.__exit__(type(interrupted), interrupted, interrupted.__traceback__)
        operation._operation_cm = None
        runtime.store.close()
        runtime._closed = True

        reopened = Runtime.open(database)
        try:
            recovered = reopened.store.get_human_request(request.request_id)
            assert recovered is not None
            assert recovered.status == HumanRequestStatus.PENDING
            assert recovered.decision == {
                "delivery_committed": False,
                "provider_not_dispatched": True,
                "startup_recovered": True,
            }
            assert reopened.store.get_external_effect(effect_id) is None
        finally:
            reopened.close()
    finally:
        if not runtime._closed:
            runtime.close()

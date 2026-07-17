from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest

import agent_libos.sdk.protected_operations as protected_operations_module
from agent_libos import Runtime
from agent_libos.models import (
    CapabilityRight,
    DataFlowContext,
    DataFlowDirection,
    DataLabels,
    DataSink,
    EventType,
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    HumanRequest,
    HumanRequestStatus,
    ObjectMetadata,
    ObjectPatch,
    ObjectType,
    ResourceUsage,
    SinkTrustLevel,
    SinkTrustRule,
)
from agent_libos.models.exceptions import CapabilityDenied, ValidationError
from agent_libos.sdk import (
    PostProviderFailureMode,
    ProtectedOperationContract,
    ProtectedOperationEvidence,
    ProtectedOperationInvocation,
    ProtectedOperationProtocolError,
    ProviderEffectNotStartedResult,
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


def _ingress_setup(runtime):
    pid, capability, base_contract, base_invocation = _setup(runtime)
    context = DataFlowContext(
        labels=DataLabels(
            sensitivity="secret",
            trust_level="untrusted",
            integrity="untrusted",
            origin="external:test-provider",
        )
    )
    contract = replace(
        base_contract,
        name="primitive.test.ingress",
        data_flow_direction=DataFlowDirection.INGRESS,
    )
    runtime.protected_operations.register_contract(contract)
    invocation = replace(
        base_invocation,
        data_flow_ingress_context=context,
    )
    return pid, capability, contract, invocation, context


@pytest.mark.parametrize(
    "direction",
    [DataFlowDirection.INGRESS, DataFlowDirection.BIDIRECTIONAL],
)
def test_sdk_rejects_ingress_without_trusted_context_before_effect_intent(
    direction: DataFlowDirection,
) -> None:
    with temporary_runtime() as runtime:
        pid, _capability, base_contract, invocation = _setup(runtime)
        contract = replace(
            base_contract,
            name=f"primitive.test.missing_ingress.{direction.value}",
            data_flow_direction=direction,
        )
        runtime.protected_operations.register_contract(contract)

        with pytest.raises(ValidationError, match="trusted DataFlowContext"):
            with runtime.protected_operations.start(
                contract,
                invocation,
                provider=_Provider(),
            ):
                pass

        assert runtime.store.list_external_effects(pid=pid) == []


@pytest.mark.parametrize(
    "direction",
    [DataFlowDirection.NONE, DataFlowDirection.EGRESS],
)
def test_sdk_rejects_ingress_context_for_non_ingress_contract(
    direction: DataFlowDirection,
) -> None:
    with temporary_runtime() as runtime:
        pid, _capability, base_contract, invocation = _setup(runtime)
        contract = replace(
            base_contract,
            name=f"primitive.test.unexpected_ingress.{direction.value}",
            data_flow_direction=direction,
        )
        runtime.protected_operations.register_contract(contract)
        invocation = replace(
            invocation,
            data_flow_ingress_context=DataFlowContext(),
        )

        with pytest.raises(ValidationError, match="non-ingress protected operation"):
            with runtime.protected_operations.start(
                contract,
                invocation,
                provider=_Provider(),
            ):
                pass

        assert runtime.store.list_external_effects(pid=pid) == []


def test_sdk_sync_ingress_observes_once_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    with temporary_runtime() as runtime:
        pid, _capability, contract, invocation, context = _ingress_setup(runtime)
        observed: list[DataFlowContext] = []
        original = runtime.data_flow.observe_ingress

        def observe(selected: DataFlowContext) -> DataFlowContext:
            observed.append(selected)
            return original(selected)

        monkeypatch.setattr(runtime.data_flow, "observe_ingress", observe)
        with runtime.protected_operations.start(
            contract,
            invocation,
            provider=_Provider(),
        ) as operation:
            result = operation.call(
                ProviderPhase("read", information_flow=True),
                lambda: "ok",
            )
            operation.complete(result, _evidence(pid))

        assert observed == [context]


def test_sdk_sync_ingress_observes_once_on_ambiguous_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with temporary_runtime() as runtime:
        _pid, _capability, contract, invocation, context = _ingress_setup(runtime)
        observed: list[DataFlowContext] = []
        original = runtime.data_flow.observe_ingress

        def observe(selected: DataFlowContext) -> DataFlowContext:
            observed.append(selected)
            return original(selected)

        monkeypatch.setattr(runtime.data_flow, "observe_ingress", observe)
        with pytest.raises(RuntimeError, match="ambiguous"):
            with runtime.protected_operations.start(
                contract,
                invocation,
                provider=_Provider(),
            ) as operation:
                operation.call(
                    ProviderPhase("read", information_flow=True),
                    lambda: (_ for _ in ()).throw(RuntimeError("ambiguous")),
                )

        assert observed == [context]


@pytest.mark.parametrize("as_result", [False, True])
def test_sdk_ingress_does_not_observe_certified_not_started(
    monkeypatch: pytest.MonkeyPatch,
    as_result: bool,
) -> None:
    with temporary_runtime() as runtime:
        _pid, _capability, contract, invocation, _context = _ingress_setup(runtime)
        observed: list[DataFlowContext] = []
        original = runtime.data_flow.observe_ingress

        def observe(selected: DataFlowContext) -> DataFlowContext:
            observed.append(selected)
            return original(selected)

        monkeypatch.setattr(runtime.data_flow, "observe_ingress", observe)
        error = ProviderEffectNotStarted("certified not started")

        if as_result:
            marker = ProviderEffectNotStartedResult(error=error, result="not-started")
            with runtime.protected_operations.start(
                contract,
                invocation,
                provider=_Provider(),
            ) as operation:
                assert operation.call(
                    ProviderPhase("read", information_flow=True),
                    lambda: marker,
                ) is marker
        else:
            with pytest.raises(ProviderEffectNotStarted, match="certified not started"):
                with runtime.protected_operations.start(
                    contract,
                    invocation,
                    provider=_Provider(),
                ) as operation:
                    operation.call(
                        ProviderPhase("read", information_flow=True),
                        lambda: (_ for _ in ()).throw(error),
                    )

        assert observed == []


def test_sdk_async_ingress_observes_once_on_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with temporary_runtime() as runtime:
        pid, _capability, contract, invocation, context = _ingress_setup(runtime)
        observed: list[DataFlowContext] = []
        original = runtime.data_flow.observe_ingress

        def observe(selected: DataFlowContext) -> DataFlowContext:
            observed.append(selected)
            return original(selected)

        monkeypatch.setattr(runtime.data_flow, "observe_ingress", observe)

        async def run() -> None:
            async def success() -> str:
                return "ok"

            with runtime.protected_operations.start(
                contract,
                invocation,
                provider=_Provider(),
            ) as operation:
                result = await operation.acall(
                    ProviderPhase("read", information_flow=True),
                    success,
                )
                operation.complete(result, _evidence(pid))

            _pid, _capability, failure_contract, failure_invocation, _context = (
                _ingress_setup(runtime)
            )

            async def failure() -> str:
                raise RuntimeError("async ambiguous")

            with pytest.raises(RuntimeError, match="async ambiguous"):
                with runtime.protected_operations.start(
                    failure_contract,
                    failure_invocation,
                    provider=_Provider(),
                ) as operation:
                    await operation.acall(
                        ProviderPhase("read", information_flow=True),
                        failure,
                    )

        asyncio.run(run())

        assert observed == [context, context]


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
        failure_settlements: list[tuple[str, str]] = []
        invocation = replace(
            invocation,
            failure_settlement=lambda error, phase: failure_settlements.append(
                (type(error).__name__, phase)
            ),
        )

        with pytest.raises(ProviderEffectNotStarted):
            with runtime.protected_operations.start(contract, invocation, provider=provider) as operation:
                operation.call(
                    ProviderPhase("read", information_flow=True),
                    lambda: (_ for _ in ()).throw(ProviderEffectNotStarted("not started")),
                )

        assert runtime.store.get_capability(capability.cap_id).uses_remaining == 1
        assert runtime.store.list_external_effects(pid=invocation.pid) == []
        assert failure_settlements == []
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


def test_sdk_unknown_effect_preserves_payload_free_data_flow_evidence() -> None:
    sentinel = "UNKNOWN_EFFECT_DATA_FLOW_SENTINEL"
    with temporary_runtime() as runtime:
        pid, _capability, base_contract, base_invocation = _setup(runtime)
        source = runtime.memory.create_object(
            pid,
            ObjectType.EVIDENCE,
            {"value": sentinel},
            metadata=ObjectMetadata(sensitivity="secret"),
        )
        sink = DataSink("filesystem:workspace:unknown-effect.txt")
        trust = runtime.data_flow.register_sink_trust(
            SinkTrustRule(
                pattern=sink.identity,
                trust_level=SinkTrustLevel.TRUSTED,
                max_sensitivity="secret",
            ),
            actor="test.host",
            require_capability=False,
        )
        flow_context = runtime.data_flow.context_from_source_oids(pid, [source.oid])
        contract = replace(
            base_contract,
            name="primitive.test.data_flow_unknown",
            data_flow_direction=DataFlowDirection.EGRESS,
        )
        runtime.protected_operations.register_contract(contract)
        invocation = replace(
            base_invocation,
            target=sink.identity,
            data_sink=sink,
            data_flow_context=flow_context,
            data_flow_payload={"content": sentinel},
            data_flow_operation="test.data_flow_unknown",
        )

        with pytest.raises(RuntimeError, match="ambiguous provider failure"):
            with runtime.protected_operations.start(
                contract,
                invocation,
                provider=_Provider(),
            ) as operation:
                operation.call(
                    ProviderPhase("send", information_flow=True),
                    lambda: (_ for _ in ()).throw(
                        RuntimeError("ambiguous provider failure")
                    ),
                )

        effect = runtime.store.list_external_effects(pid=pid)[0]
        assert effect.transaction_state == "unknown"
        evidence = effect.provider_metadata["data_flow"]
        assert evidence["decision_id"]
        assert evidence["sink"] == sink.identity
        assert evidence["labels"]["sensitivity"] == "secret"
        assert evidence["source_refs"] == [flow_context.source_refs[0].to_dict()]
        assert evidence["trust_id"] == trust.trust_id
        assert evidence["trust_sha256"] == trust.spec_hash
        assert evidence["registry_generation"] == trust.generation
        assert evidence["payload_sha256"]
        assert sentinel not in str(effect.provider_metadata)


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


def test_sdk_reauthorizes_reusable_capability_after_invocation_prepare() -> None:
    with temporary_runtime() as runtime:
        pid = runtime.process.spawn(goal="revalidate reusable protected authority")
        capability = runtime.capability.issue_trusted(
            pid,
            "test:reusable",
            [CapabilityRight.READ],
            issued_by="test",
        )
        decision = runtime.capability.require(
            pid,
            "test:reusable",
            CapabilityRight.READ,
            {"operation": "read", "stable_argument": "original"},
            consume=False,
        )
        contract = ProtectedOperationContract(
            name="primitive.test.reusable_read",
            provider="test",
            operation="read",
            evidence_roles=("audit", "event", "effect"),
            resource_policy=ResourcePolicy.NONE,
            information_flow=True,
        )
        runtime.protected_operations.register_contract(contract)
        provider_calls = 0

        def revoke_during_prepare() -> None:
            runtime.capability.revoke(
                capability.cap_id,
                revoked_by="test",
                reason="race regression",
                require_authority=False,
            )

        invocation = ProtectedOperationInvocation(
            pid=pid,
            actor=pid,
            target="test:reusable",
            decisions=(decision,),
            canonical_args={"stable_argument": "original"},
            observation={"argument_sha256": "safe"},
            prepare=revoke_during_prepare,
        )

        with pytest.raises(CapabilityDenied, match="changed before protected dispatch"):
            with runtime.protected_operations.start(
                contract,
                invocation,
                provider=_Provider(),
            ) as operation:
                provider_calls += 1
                operation.call(ProviderPhase("read", information_flow=True), lambda: "sent")

        assert provider_calls == 0
        assert runtime.store.list_external_effects(pid=pid) == []


def test_sdk_dispatch_reauthorizes_reusable_capability_after_prepare() -> None:
    with temporary_runtime() as runtime:
        pid = runtime.process.spawn(goal="revalidate reusable authority at dispatch")
        capability = runtime.capability.issue_trusted(
            pid,
            "test:reusable-dispatch",
            [CapabilityRight.READ],
            issued_by="test",
        )
        decision = runtime.capability.require(
            pid,
            "test:reusable-dispatch",
            CapabilityRight.READ,
            {"operation": "read", "stable_argument": "original"},
            consume=False,
        )
        contract = ProtectedOperationContract(
            name="primitive.test.reusable_dispatch",
            provider="test",
            operation="read",
            evidence_roles=("audit", "event", "effect"),
            resource_policy=ResourcePolicy.NONE,
            information_flow=True,
        )
        runtime.protected_operations.register_contract(contract)
        invocation = ProtectedOperationInvocation(
            pid=pid,
            actor=pid,
            target="test:reusable-dispatch",
            decisions=(decision,),
            canonical_args={"stable_argument": "original"},
            observation={"argument_sha256": "safe"},
        )
        provider_calls = 0

        with pytest.raises(CapabilityDenied, match="changed before protected dispatch"):
            with runtime.protected_operations.start(
                contract,
                invocation,
                provider=_Provider(),
            ) as operation:
                runtime.capability.revoke(
                    capability.cap_id,
                    revoked_by="test",
                    reason="post-prepare dispatch race regression",
                    require_authority=False,
                )

                def provider_call() -> str:
                    nonlocal provider_calls
                    provider_calls += 1
                    return "sent"

                result = operation.call(
                    ProviderPhase("read", information_flow=True),
                    provider_call,
                )
                operation.complete(result, _evidence(pid))

        assert provider_calls == 0
        assert runtime.store.list_external_effects(pid=pid) == []


def test_sdk_repersists_data_flow_denial_after_prepare_transaction_rollback() -> None:
    sentinel = "DATA_FLOW_ROLLBACK_SENTINEL"
    with temporary_runtime() as runtime:
        pid, capability, base_contract, base_invocation = _setup(runtime)
        sink = DataSink("jsonrpc:rollback-regression:read")
        contract = replace(
            base_contract,
            name="primitive.test.data_flow_rollback",
            data_flow_direction=DataFlowDirection.EGRESS,
        )
        runtime.protected_operations.register_contract(contract)

        def change_registry_generation() -> None:
            runtime.data_flow.register_sink_trust(
                SinkTrustRule(pattern=sink.identity),
                actor="test.host",
                require_capability=False,
            )

        invocation = replace(
            base_invocation,
            data_sink=sink,
            data_flow_context=DataFlowContext(),
            data_flow_payload=sentinel,
            data_flow_operation="test.data_flow_rollback",
            prepare=change_registry_generation,
        )
        provider_calls = 0

        with pytest.raises(CapabilityDenied, match="registry generation changed") as raised:
            with runtime.protected_operations.start(
                contract,
                invocation,
                provider=_Provider(),
            ) as operation:
                provider_calls += 1
                operation.call(ProviderPhase("read", information_flow=True), lambda: "sent")

        assert provider_calls == 0
        assert runtime.store.get_capability(capability.cap_id).uses_remaining == 1
        assert runtime.store.list_external_effects(pid=pid) == []
        denied = runtime.store.list_data_flow_decisions(pid=pid, outcome="deny")
        assert len(denied) == 1
        assert denied[0].decision_id in str(raised.value)
        audit = next(
            record
            for record in runtime.audit.trace()
            if record.action == "data_flow.egress"
            and record.decision.get("outcome") == "deny"
        )
        event = next(
            item
            for item in runtime.events.list(target=f"data_flow_sink:{sink.identity}")
            if item.type == EventType.DATA_FLOW_DECISION
            and item.payload.get("outcome") == "deny"
        )
        assert sentinel not in str(denied[0])
        assert sentinel not in str(audit.decision)
        assert sentinel not in str(event.payload)


@pytest.mark.parametrize("mutation_point", ("before_first_phase", "between_phases"))
def test_sdk_revalidates_data_flow_at_each_provider_dispatch(
    mutation_point: str,
) -> None:
    with temporary_runtime() as runtime:
        pid, capability, base_contract, base_invocation = _setup(runtime)
        source = runtime.memory.create_object(
            pid,
            ObjectType.EVIDENCE,
            {"value": "before-dispatch"},
            metadata=ObjectMetadata(sensitivity="secret"),
            immutable=False,
        )
        sink = DataSink("test:dispatch-revalidation")
        runtime.data_flow.register_sink_trust(
            SinkTrustRule(
                pattern=sink.identity,
                trust_level=SinkTrustLevel.TRUSTED,
                max_sensitivity="secret",
            ),
            actor="test.host",
            require_capability=False,
        )
        flow_context = runtime.data_flow.context_from_source_oids(pid, [source.oid])
        contract = replace(
            base_contract,
            name="primitive.test.dispatch_revalidation",
            operation="send",
            data_flow_direction=DataFlowDirection.EGRESS,
        )
        runtime.protected_operations.register_contract(contract)
        invocation = replace(
            base_invocation,
            target=sink.identity,
            data_sink=sink,
            data_flow_context=flow_context,
            data_flow_payload={"value": "before-dispatch"},
            data_flow_operation="test.dispatch_revalidation",
        )
        send_calls = 0

        with pytest.raises(CapabilityDenied, match="source Object changed before dispatch"):
            with runtime.protected_operations.start(
                contract,
                invocation,
                provider=_Provider(),
            ) as operation:
                if mutation_point == "between_phases":
                    operation.call(
                        ProviderPhase("state", information_flow=True),
                        lambda: "state-observed",
                    )
                runtime.memory.update_object(
                    pid,
                    source,
                    ObjectPatch(payload={"value": "changed-after-prepare"}),
                )

                def send_provider() -> str:
                    nonlocal send_calls
                    send_calls += 1
                    return "sent"

                operation.call(
                    ProviderPhase("send", information_flow=True),
                    send_provider,
                )

        assert send_calls == 0
        expected_uses = 0 if mutation_point == "between_phases" else 1
        assert runtime.store.get_capability(capability.cap_id).uses_remaining == expected_uses
        effects = runtime.store.list_external_effects(pid=pid)
        if mutation_point == "between_phases":
            assert len(effects) == 1
            assert effects[0].transaction_state == "unknown"
        else:
            assert effects == []
        denied = runtime.store.list_data_flow_decisions(pid=pid, outcome="deny")
        assert denied and "source Object changed" in denied[-1].reason
        denial = denied[-1]
        assert any(
            record.action == "data_flow.egress"
            and record.target == sink.identity
            and record.decision.get("decision_id") == denial.decision_id
            for record in runtime.audit.trace()
        )
        assert any(
            event.type == EventType.DATA_FLOW_DECISION
            and event.payload.get("decision_id") == denial.decision_id
            for event in runtime.events.list(
                target=f"data_flow_sink:{sink.identity}",
            )
        )


def test_sdk_dispatch_linearizes_final_data_flow_check_with_registry_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with temporary_runtime() as runtime:
        pid, _capability, base_contract, base_invocation = _setup(runtime)
        source = runtime.memory.create_object(
            pid,
            ObjectType.EVIDENCE,
            {"value": "classified"},
            metadata=ObjectMetadata(sensitivity="secret"),
            immutable=False,
        )
        sink = DataSink("test:dispatch-registry-linearization")
        trust = runtime.data_flow.register_sink_trust(
            SinkTrustRule(
                pattern=sink.identity,
                trust_level=SinkTrustLevel.TRUSTED,
                max_sensitivity="secret",
            ),
            actor="test.host",
            require_capability=False,
        )
        contract = replace(
            base_contract,
            name="primitive.test.dispatch_registry_linearization",
            operation="send",
            data_flow_direction=DataFlowDirection.EGRESS,
        )
        runtime.protected_operations.register_contract(contract)
        invocation = replace(
            base_invocation,
            target=sink.identity,
            data_sink=sink,
            data_flow_context=runtime.data_flow.context_from_source_oids(
                pid,
                [source.oid],
            ),
            data_flow_payload={"value": "classified"},
            data_flow_operation="test.dispatch_registry_linearization",
        )
        final_authorization_returned = Event()
        mutation_started = Event()
        mutation_committed = Event()
        mutation_errors: list[BaseException] = []
        order: list[str] = []
        provider_calls = 0
        original_mark_dispatched = (
            protected_operations_module.mark_external_effect_dispatched
        )

        def track_dispatch_mark(*args, **kwargs):
            result = original_mark_dispatched(*args, **kwargs)
            order.append("dispatch_marked")
            return result

        monkeypatch.setattr(
            protected_operations_module,
            "mark_external_effect_dispatched",
            track_dispatch_mark,
        )

        with runtime.protected_operations.start(
            contract,
            invocation,
            provider=_Provider(),
        ) as operation:
            original_authorize_egress = runtime.data_flow.authorize_egress

            def authorize_then_release_mutator(**kwargs):
                result = original_authorize_egress(**kwargs)
                final_authorization_returned.set()
                if not mutation_started.wait(timeout=5):
                    raise AssertionError("Sink registry mutator did not start")
                # Without one shared dispatch critical section the mutation can
                # commit here, after the final allow decision but before the
                # external-effect dispatch mark. With the fix it remains blocked
                # on the RuntimeStore lock until dispatch is durably marked.
                mutation_committed.wait(timeout=1)
                return result

            monkeypatch.setattr(
                runtime.data_flow,
                "authorize_egress",
                authorize_then_release_mutator,
            )

            def unregister_sink() -> None:
                try:
                    if not final_authorization_returned.wait(timeout=5):
                        raise AssertionError("final data-flow authorization did not run")
                    mutation_started.set()
                    runtime.data_flow.unregister_sink_trust(
                        sink.identity,
                        actor="test.host",
                        require_capability=False,
                    )
                    order.append("registry_mutation_committed")
                except BaseException as error:
                    mutation_errors.append(error)
                finally:
                    mutation_committed.set()

            worker = Thread(target=unregister_sink)
            worker.start()

            def provider_call() -> str:
                nonlocal provider_calls
                provider_calls += 1
                return "sent"

            result = operation.call(
                ProviderPhase("send", information_flow=True),
                provider_call,
            )
            worker.join(timeout=5)
            assert not worker.is_alive()
            if mutation_errors:
                raise mutation_errors[0]
            operation.complete(result, _evidence(pid))

        assert provider_calls == 1
        assert order.index("dispatch_marked") < order.index(
            "registry_mutation_committed"
        )
        assert runtime.data_flow.inspect_sink_trust(sink.identity) is None
        effect = runtime.store.list_external_effects(pid=pid)[0]
        assert effect.transaction_state == "committed"
        assert effect.provider_metadata["data_flow"]["registry_generation"] == trust.generation
        assert runtime.store.get_sink_trust_generation() == trust.generation + 1


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
        failure_settlements: list[tuple[str, str]] = []
        invocation = replace(
            invocation,
            failure_settlement=lambda error, phase: failure_settlements.append(
                (type(error).__name__, phase)
            ),
        )
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
        assert failure_settlements == [("RuntimeError", "completion_settlement")]
        effect = runtime.store.list_external_effects(pid=pid)[0]
        assert effect.effect_state == "pending"
        assert effect.transaction_state == "dispatched"


def test_sdk_failure_settlement_error_is_chained_from_provider_error() -> None:
    with temporary_runtime() as runtime:
        _pid, _capability, contract, invocation = _setup(runtime)

        def fail_settlement(_error: BaseException, _phase: str) -> None:
            raise ValidationError("failure settlement failed")

        invocation = replace(invocation, failure_settlement=fail_settlement)
        with pytest.raises(ValidationError, match="failure settlement failed") as raised:
            with runtime.protected_operations.start(
                contract,
                invocation,
                provider=_Provider(),
            ) as operation:
                operation.call(
                    ProviderPhase("read", information_flow=True),
                    lambda: (_ for _ in ()).throw(RuntimeError("provider failed")),
                )

        assert isinstance(raised.value.__cause__, RuntimeError)


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
    store_closed = False
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
        store_closed = True

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
        if not store_closed:
            runtime.close()


def test_sdk_recovery_restores_prepared_human_output_claim(tmp_path: Path) -> None:
    database = tmp_path / "prepared-human.sqlite"
    runtime = Runtime.open(database)
    store_closed = False
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
            data_sink=DataSink("human:owner:terminal"),
            data_flow_context=DataFlowContext(),
            data_flow_payload="secret",
            data_flow_operation="human.output",
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
        store_closed = True

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
        if not store_closed:
            runtime.close()

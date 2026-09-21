#!/usr/bin/env python3
"""Exercise an ingress provider boundary without network, credentials, or an LLM."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_libos import Runtime
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models import (
    CapabilityRight,
    DataFlowDirection,
    EventType,
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    ResourceUsage,
)
from agent_libos.models.exceptions import CapabilityDenied
from agent_libos.sdk import (
    ProtectedOperationContract,
    ProtectedOperationEvidence,
    ProtectedOperationInvocation,
    ProviderPhase,
    ResourcePolicy,
    ResourceSettlement,
)
from agent_libos.substrate import LocalResourceProviderSubstrate


RESOURCE = "example:notice"
CONTRACT = "primitive.example.read_notice"
NOTICE = "hello"


class NoticeProvider:
    """A deterministic stand-in for one bounded, read-only Host provider."""

    def __init__(self) -> None:
        self.calls = 0

    def read_notice(self) -> str:
        self.calls += 1
        return NOTICE

    def classify_external_effect(
        self, operation: str, context: dict[str, Any], result: Any
    ) -> ExternalEffectClassification:
        if operation != "read_notice":
            raise ValueError("unsupported operation")
        return ExternalEffectClassification(
            rollback_class=ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED,
            rollback_status=ExternalEffectRollbackStatus.NOT_REQUIRED,
            state_mutation=False,
            information_flow=True,
        )


def read_notice(runtime: Runtime, pid: str, provider: NoticeProvider) -> str:
    """Primitive-like facade: authorize first, then dispatch only inside the SDK."""
    decision = runtime.capability.require(pid, RESOURCE, CapabilityRight.READ, consume=False)
    ingress = runtime.data_flow.unclassified_ingress_context(
        runtime.data_flow.current_context(), origin="external:example-notice"
    )
    invocation = ProtectedOperationInvocation(
        pid=pid,
        actor=pid,
        target=RESOURCE,
        decisions=(decision,),
        canonical_args={"notice_id": "public"},
        observation={"notice_id": "public"},
        preflight_usage=ResourceUsage(external_read_bytes=len(NOTICE.encode("utf-8"))),
        resource_source=CONTRACT,
        data_flow_ingress_context=ingress,
    )
    with runtime.protected_operations.start(CONTRACT, invocation, provider=provider) as operation:
        result = operation.call(
            ProviderPhase("read", information_flow=True), provider.read_notice
        )
        size = len(result.encode("utf-8"))
        return operation.complete(
            result,
            ProtectedOperationEvidence(
                event_type=EventType.EXTERNAL_READ,
                event_source=pid,
                event_target=RESOURCE,
                event_payload={"bytes_read": size},
                audit_action=CONTRACT,
                audit_actor=pid,
                audit_target=RESOURCE,
                audit_decision={"bytes_read": size},
                effect_metadata={"bytes_read": size},
            ),
            classification_result={"bytes_read": size},
            resource=ResourceSettlement(
                ResourceUsage(external_read_bytes=size), source=CONTRACT
            ),
        )


def run_example() -> dict[str, Any]:
    with TemporaryDirectory(prefix="agent-libos-protected-operation-") as directory:
        runtime = Runtime.open(
            ":memory:",
            config=DEFAULT_CONFIG,
            substrate=LocalResourceProviderSubstrate(Path(directory)),
        )
        try:
            runtime.protected_operations.register_contract(
                ProtectedOperationContract(
                    name=CONTRACT,
                    provider="example",
                    operation="read_notice",
                    evidence_roles=("audit", "event", "effect"),
                    resource_policy=ResourcePolicy.REQUIRED,
                    information_flow=True,
                    data_flow_direction=DataFlowDirection.INGRESS,
                )
            )
            pid = runtime.process.spawn(goal="Read one deterministic provider notice")
            provider = NoticeProvider()
            capability = runtime.capability.issue_trusted(
                pid,
                RESOURCE,
                [CapabilityRight.READ],
                issued_by="example.host",
                uses_remaining=1,
            )
            result = read_notice(runtime, pid, provider)
            assert result == NOTICE
            effects = runtime.store.list_external_effects(pid=pid)
            assert len(effects) == 1
            effect = effects[0]
            assert effect.transaction_state == "committed"
            assert effect.record_id is not None and effect.event_id is not None
            assert runtime.process.get(pid).resource_usage.external_read_bytes == 5
            saved_capability = runtime.store.get_capability(capability.cap_id)
            assert saved_capability is not None and saved_capability.uses_remaining == 0

            try:
                read_notice(runtime, pid, provider)
            except CapabilityDenied:
                pass
            else:
                raise AssertionError("the consumed one-use capability must deny a second read")
            assert provider.calls == 1
            assert len(runtime.store.list_external_effects(pid=pid)) == 1
            return {
                "result": result,
                "provider_calls": provider.calls,
                "effect_state": effect.transaction_state,
                "audit_and_event_linked": True,
                "denied_after_one_use": True,
            }
        finally:
            closed = runtime.close()
            assert closed["ok"], closed


if __name__ == "__main__":
    print(json.dumps(run_example(), sort_keys=True))

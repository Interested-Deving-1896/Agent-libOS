from __future__ import annotations

from dataclasses import asdict
from typing import Any

from agent_libos.models import (
    AuditRecord,
    Event,
    ExternalEffectClassification,
    ExternalEffectRecord,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
)
from agent_libos.models.exceptions import ValidationError
from agent_libos.storage import RuntimeStore
from agent_libos.utils.ids import new_id, utc_now


def require_external_effect_classifier(provider: Any, operation: str) -> None:
    if not callable(getattr(provider, "classify_external_effect", None)):
        raise ValidationError(
            f"provider {provider.__class__.__name__} cannot classify external effect operation {operation!r}"
        )


def classify_external_effect(
    provider: Any,
    operation: str,
    context: dict[str, Any],
    result: Any,
) -> ExternalEffectClassification:
    require_external_effect_classifier(provider, operation)
    raw = provider.classify_external_effect(operation, context, result)
    if isinstance(raw, ExternalEffectClassification):
        return raw
    if isinstance(raw, dict):
        return ExternalEffectClassification(
            rollback_class=ExternalEffectRollbackClass(str(raw["rollback_class"])),
            rollback_status=ExternalEffectRollbackStatus(str(raw["rollback_status"])),
            state_mutation=bool(raw["state_mutation"]),
            information_flow=bool(raw["information_flow"]),
            metadata=dict(raw.get("metadata") or {}),
        )
    raise ValidationError("provider external effect classifier must return ExternalEffectClassification")


def record_external_effect(
    store: RuntimeStore,
    *,
    pid: str,
    provider: str,
    operation: str,
    target: str | None,
    classification: ExternalEffectClassification,
    audit_record: AuditRecord | None,
    event: Event | None,
    metadata: dict[str, Any] | None = None,
    intent_effect_id: str | None = None,
) -> ExternalEffectRecord:
    provider_metadata = {
        **dict(classification.metadata),
        **dict(metadata or {}),
        "effect_state": "finalized",
    }
    record = ExternalEffectRecord(
        effect_id=intent_effect_id or new_id("eff"),
        record_id=audit_record.record_id if audit_record is not None else None,
        event_id=event.event_id if event is not None else None,
        pid=pid,
        provider=provider,
        operation=operation,
        target=target,
        rollback_class=classification.rollback_class,
        rollback_status=classification.rollback_status,
        state_mutation=classification.state_mutation,
        information_flow=classification.information_flow,
        provider_metadata=provider_metadata,
        created_at=utc_now(),
        effect_state="finalized",
    )
    if intent_effect_id is None:
        store.insert_external_effect(record)
    elif not store.finalize_external_effect(intent_effect_id, record):
        raise ValidationError(
            "external effect intent was missing, already finalized, or did not match its provider boundary"
        )
    return record


def begin_external_effect_intent(
    store: RuntimeStore,
    *,
    pid: str,
    provider: str,
    operation: str,
    target: str | None,
    state_mutation: bool,
    information_flow: bool,
    metadata: dict[str, Any] | None = None,
) -> ExternalEffectRecord:
    """Persist conservative evidence immediately before a provider boundary."""

    record = ExternalEffectRecord(
        effect_id=new_id("effintent"),
        record_id=None,
        event_id=None,
        pid=pid,
        provider=provider,
        operation=operation,
        target=target,
        rollback_class=ExternalEffectRollbackClass.UNKNOWN,
        rollback_status=ExternalEffectRollbackStatus.UNKNOWN,
        state_mutation=state_mutation,
        information_flow=information_flow,
        provider_metadata={
            **dict(metadata or {}),
            "effect_state": "pending",
            "outcome": "unknown_after_provider_boundary",
        },
        created_at=utc_now(),
        effect_state="pending",
    )
    store.insert_external_effect(record)
    return record


def abandon_external_effect_intent(store: RuntimeStore, intent_effect_id: str | None) -> None:
    """Remove an intent only when the provider certifies the effect never started."""

    if intent_effect_id is not None:
        if not store.abandon_external_effect_intent(intent_effect_id):
            raise ValidationError("external effect intent was missing or already finalized")


def external_effect_to_json(record: ExternalEffectRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["rollback_class"] = record.rollback_class.value
    payload["rollback_status"] = record.rollback_status.value
    return payload


def external_effect_summary(records: list[ExternalEffectRecord]) -> dict[str, Any]:
    by_class: dict[str, int] = {}
    by_provider_operation: dict[str, int] = {}
    state_mutations = 0
    information_flows = 0
    by_state: dict[str, int] = {}
    for record in records:
        by_class[record.rollback_class.value] = by_class.get(record.rollback_class.value, 0) + 1
        key = f"{record.provider}.{record.operation}"
        by_provider_operation[key] = by_provider_operation.get(key, 0) + 1
        state_mutations += int(record.state_mutation)
        information_flows += int(record.information_flow)
        by_state[record.effect_state] = by_state.get(record.effect_state, 0) + 1
    return {
        "total": len(records),
        "by_rollback_class": by_class,
        "by_provider_operation": by_provider_operation,
        "state_mutations": state_mutations,
        "information_flows": information_flows,
        "by_state": by_state,
        "pending": by_state.get("pending", 0),
    }

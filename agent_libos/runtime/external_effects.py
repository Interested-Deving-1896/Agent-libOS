from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
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
from agent_libos.runtime.effect_binding import (
    canonical_effect_hash,
    current_approval_effect_binding,
)
from agent_libos.utils.serde import dumps


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
    intent = store.get_external_effect(intent_effect_id) if intent_effect_id is not None else None
    # Rollback support and provider outcome are separate axes. A provider may
    # confirm that an effect committed while being unable to classify how it
    # could be rolled back. Only an explicitly unknown outcome makes the
    # transaction outcome unknown.
    transaction_state = (
        "unknown"
        if str(provider_metadata.get("outcome") or "").startswith("unknown")
        else "committed"
    )
    receipt = provider_metadata.get("provider_receipt")
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
        transaction_state=transaction_state,
        provider_receipt=dict(receipt) if isinstance(receipt, dict) else {},
        canonical_args_hash=intent.canonical_args_hash if intent is not None else None,
        idempotency_key=intent.idempotency_key if intent is not None else None,
        updated_at=utc_now(),
    )
    if intent_effect_id is None:
        store.insert_external_effect(record)
    elif not store.finalize_external_effect(intent_effect_id, record):
        raise ValidationError(
            "external effect intent was missing, already finalized, or did not match its provider boundary"
        )
    operations = getattr(store, "operation_manager", None)
    if operations is not None:
        operations.link_evidence(
            "external_effect",
            record.effect_id,
            "effect",
            metadata={"effect_state": "finalized", "provider": provider, "operation": operation},
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
    idempotency_key: str | None = None,
) -> ExternalEffectRecord:
    """Persist conservative evidence immediately before a provider boundary."""

    manifests = getattr(store, "authority_manifest_manager", None)
    manifest = None
    if manifests is not None:
        manifests.assert_effect(pid, f"{provider}.{operation}")
        manifest = manifests.get_for_process(pid)
    context = dict((metadata or {}).get("context") or metadata or {})
    approval_binding = current_approval_effect_binding(store)
    effect_id = approval_binding["effect_id"] if approval_binding is not None else new_id("effintent")
    args_hash = (
        approval_binding["canonical_args_hash"]
        if approval_binding is not None
        else canonical_effect_hash(context)
    )
    operations = getattr(store, "operation_manager", None)
    operation_id = operations.current_id() if operations is not None else None
    selected_idempotency_key = idempotency_key or hashlib.sha256(
        dumps(
            {
                "operation_id": operation_id or effect_id,
                "provider": provider,
                "operation": operation,
                "target": target,
                "canonical_args_hash": args_hash,
            }
        ).encode("utf-8")
    ).hexdigest()
    existing = next(
        (
            item
            for item in store.list_external_effects(pid=pid)
            if item.idempotency_key == selected_idempotency_key
        ),
        None,
    )
    if existing is not None:
        raise ValidationError(
            "duplicate external effect dispatch blocked by idempotency key: "
            f"{selected_idempotency_key} existing_effect={existing.effect_id} "
            f"state={existing.transaction_state}"
        )
    now = utc_now()
    selected_metadata = dict(metadata or {})
    if information_flow:
        raw_labels = selected_metadata.get("data_labels")
        labels = {
            str(key): value
            for key, value in dict(raw_labels or {}).items()
            if str(key)
            in {
                "sensitivity",
                "trust_level",
                "integrity",
                "origin",
                "tenant",
                "principal",
            }
        }
        selected_metadata["information_flow_evidence"] = {
            "mode": "observe_only",
            "labels": labels,
            "manifest_policy": dict(manifest.data_flow_policy) if manifest is not None else {},
        }
    record = ExternalEffectRecord(
        effect_id=effect_id,
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
            **selected_metadata,
            "effect_state": "pending",
            "outcome": "unknown_after_provider_boundary",
        },
        created_at=now,
        effect_state="pending",
        transaction_state="prepared",
        canonical_args_hash=args_hash,
        idempotency_key=selected_idempotency_key,
        updated_at=now,
    )
    try:
        store.insert_external_effect(record)
    except Exception as exc:
        raced = next(
            (
                item
                for item in store.list_external_effects(pid=pid)
                if item.idempotency_key == selected_idempotency_key
            ),
            None,
        )
        if raced is not None:
            raise ValidationError(
                "duplicate external effect dispatch blocked by concurrent idempotency claim: "
                f"{selected_idempotency_key} existing_effect={raced.effect_id}"
            ) from exc
        raise
    operations = getattr(store, "operation_manager", None)
    if operations is not None:
        operations.expect("effect", "event", "audit")
        operations.link_evidence(
            "external_effect",
            record.effect_id,
            "effect",
            metadata={"effect_state": "pending", "provider": provider, "operation": operation},
        )
    # The caller invokes this helper immediately before entering a provider
    # boundary. Persist the dispatch transition separately so a crash cannot
    # erase whether reconciliation is required.
    return mark_external_effect_dispatched(store, record.effect_id)


def mark_external_effect_dispatched(store: RuntimeStore, effect_id: str) -> ExternalEffectRecord:
    current = store.get_external_effect(effect_id)
    if current is None:
        raise ValidationError(f"external effect intent not found: {effect_id}")
    metadata = {**dict(current.provider_metadata), "transaction_state": "dispatched"}
    if not store.transition_external_effect(
        effect_id,
        expected_states=("prepared", "authorized", "approved"),
        transaction_state="dispatched",
        provider_metadata=metadata,
        updated_at=utc_now(),
    ):
        refreshed = store.get_external_effect(effect_id)
        if refreshed is None or refreshed.transaction_state != "dispatched":
            raise ValidationError(f"external effect intent cannot be dispatched: {effect_id}")
    return store.get_external_effect(effect_id) or current


def mark_external_effect_unknown(
    store: RuntimeStore,
    effect_id: str,
    *,
    reason: str,
    provider_receipt: dict[str, Any] | None = None,
) -> ExternalEffectRecord:
    current = store.get_external_effect(effect_id)
    if current is None:
        raise ValidationError(f"external effect intent not found: {effect_id}")
    metadata = {
        **dict(current.provider_metadata),
        "outcome": "unknown",
        "reconciliation_reason": reason,
        "transaction_state": "unknown",
    }
    if not store.transition_external_effect(
        effect_id,
        expected_states=("prepared", "authorized", "approved", "dispatched", "unknown"),
        transaction_state="unknown",
        provider_metadata=metadata,
        provider_receipt=provider_receipt,
        updated_at=utc_now(),
    ):
        raise ValidationError(f"external effect intent cannot become unknown: {effect_id}")
    return store.get_external_effect(effect_id) or current


def reconcile_pending_external_effects(store: RuntimeStore, substrate: Any) -> list[ExternalEffectRecord]:
    """Reconcile without replay; unsupported providers remain explicitly unknown."""

    reconciled: list[ExternalEffectRecord] = []
    for effect in store.list_external_effects():
        if effect.effect_state != "pending":
            continue
        provider = getattr(substrate, effect.provider, None)
        reconcile = getattr(provider, "reconcile_external_effect", None)
        if not callable(reconcile):
            reconciled.append(
                mark_external_effect_unknown(
                    store,
                    effect.effect_id,
                    reason="provider_does_not_support_reconciliation",
                )
            )
            continue
        try:
            result = reconcile(effect)
        except Exception as exc:
            reconciled.append(
                mark_external_effect_unknown(
                    store,
                    effect.effect_id,
                    reason=f"provider_reconciliation_error:{type(exc).__name__}",
                )
            )
            continue
        if not isinstance(result, dict):
            reconciled.append(
                mark_external_effect_unknown(store, effect.effect_id, reason="invalid_reconciliation_result")
            )
            continue
        state = str(result.get("state") or "unknown")
        receipt = result.get("provider_receipt")
        if state not in {"committed", "failed", "compensated", "unknown"}:
            state = "unknown"
        metadata = {
            **dict(effect.provider_metadata),
            "reconciled": True,
            "transaction_state": state,
            "outcome": state,
        }
        selected_receipt = dict(receipt) if isinstance(receipt, dict) else {}
        if state in {"committed", "failed", "compensated"}:
            settled = replace(
                effect,
                effect_state="finalized",
                transaction_state=state,
                provider_metadata=metadata,
                provider_receipt=selected_receipt,
                updated_at=utc_now(),
            )
            if not store.finalize_external_effect(effect.effect_id, settled):
                raise ValidationError(f"external effect reconciliation raced: {effect.effect_id}")
        elif not store.transition_external_effect(
            effect.effect_id,
            expected_states=("prepared", "authorized", "approved", "dispatched", "unknown"),
            transaction_state=state,
            provider_metadata=metadata,
            provider_receipt=selected_receipt,
            updated_at=utc_now(),
        ):
            raise ValidationError(f"external effect reconciliation raced: {effect.effect_id}")
        reconciled.append(store.get_external_effect(effect.effect_id) or effect)
    return reconciled


def abandon_external_effect_intent(store: RuntimeStore, intent_effect_id: str | None) -> None:
    """Remove an intent only when the provider certifies the effect never started."""

    if intent_effect_id is not None:
        if not store.abandon_external_effect_intent(intent_effect_id):
            raise ValidationError("external effect intent was missing or already finalized")
        operations = getattr(store, "operation_manager", None)
        if operations is not None:
            operations.link_evidence(
                "external_effect",
                intent_effect_id,
                "result",
                metadata={"outcome": "not_started", "effect_state": "abandoned"},
            )


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

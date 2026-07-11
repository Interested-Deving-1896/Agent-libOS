from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from typing import Any, Iterable

from agent_libos.models import (
    ContextMaterializationManifest,
    OperationOutcome,
    OperationRecord,
)
from agent_libos.models.exceptions import NotFound, ValidationError
from agent_libos.storage import RuntimeStore
from agent_libos.tools.observability import (
    SENSITIVE_OBSERVABILITY_KEYS,
    sanitize_for_observability,
)


_RESOLVE_TYPES: dict[str, tuple[str, ...]] = {
    "call": ("tool_call", "llm_call", "provider_call", "remote_request"),
    "effect": ("external_effect",),
    "request": ("human_request", "llm_request", "remote_request"),
    "audit": ("audit",),
    "event": ("event",),
    "reservation": ("capability_reservation",),
    "context": ("context_manifest",),
}

_EXPLAIN_SENSITIVE_KEYS = frozenset(
    {
        *SENSITIVE_OBSERVABILITY_KEYS,
        "arguments_preview",
        "argv",
        "command",
        "command_line",
        "content_preview",
        "env",
        "environment",
        "error",
        "exception",
        "headers",
        "params_preview",
        "prompt_preview",
        "provider_argv",
        "provider_payload",
        "request_body",
        "response_body",
        "stdio_args",
        "stdio_env",
        "tool_calls_preview",
    }
)


class ExplainManager:
    SCHEMA_VERSION = 1
    DEFAULT_LIST_LIMIT = 100
    LIST_HARD_LIMIT = 500
    DEFAULT_EVIDENCE_LIMIT = 200
    EVIDENCE_HARD_LIMIT = 2_000

    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.store: RuntimeStore = runtime.store

    def list_operations(
        self,
        pid: str,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if self.store.get_process(pid) is None:
            raise NotFound(f"process not found: {pid}")
        selected_limit = self._bounded_limit(limit, self.DEFAULT_LIST_LIMIT, self.LIST_HARD_LIMIT)
        rows = self.store.list_operations(
            pid=pid,
            roots_only=True,
            limit=selected_limit + 1,
            cursor=cursor,
        )
        truncated = len(rows) > selected_limit
        selected = rows[:selected_limit]
        return {
            "schema_version": self.SCHEMA_VERSION,
            "pid": pid,
            "roots_only": True,
            "operations": [self._operation_summary(record) for record in selected],
            "presentation_truncated": truncated,
            "next_cursor": selected[-1].operation_id if truncated and selected else None,
        }

    def explain_operation(
        self,
        operation_id: str,
        evidence_limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        selected = self.store.get_operation(operation_id)
        if selected is None:
            raise NotFound(f"operation not found: {operation_id}")
        operations = self.store.list_operations(root_operation_id=selected.root_operation_id)
        operations.sort(key=lambda item: (item.started_at, item.operation_id))
        operation_ids = [item.operation_id for item in operations]
        selected_limit = self._bounded_limit(
            evidence_limit,
            self.DEFAULT_EVIDENCE_LIMIT,
            self.EVIDENCE_HARD_LIMIT,
        )
        all_links = self.store.list_operation_evidence(operation_ids=operation_ids)
        grouped_all = self._group_links(all_links)
        evidence_rows = [
            (
                self._evidence_payload(
                    evidence_type,
                    evidence_id,
                    data["roles"],
                    data["metadata"],
                    data["occurred_at"],
                ),
                data["cursor"],
                data["link_ids"],
            )
            for (evidence_type, evidence_id), data in grouped_all.items()
        ]
        evidence_rows.sort(
            key=lambda item: (
                str(item[0].get("occurred_at") or ""),
                item[0]["evidence_type"],
                item[0]["evidence_id"],
            )
        )
        start = 0
        if cursor is not None:
            start = len(evidence_rows)
            for index, (_payload, representative, link_ids) in enumerate(evidence_rows):
                if cursor == representative or cursor in link_ids:
                    start = index + 1
                    break
        selected_rows = evidence_rows[start : start + selected_limit + 1]
        truncated = len(selected_rows) > selected_limit
        page_rows = selected_rows[:selected_limit]
        evidence = [payload for payload, _representative, _link_ids in page_rows]
        missing = self._missing_evidence(operations, all_links)
        uncertainties = self._uncertainties(operations, grouped_all)
        return {
            "schema_version": self.SCHEMA_VERSION,
            "lookup": {"kind": "operation", "id": operation_id},
            "selected_operation_id": operation_id,
            "root": self._operation_summary(self._root_operation(operations, selected.root_operation_id)),
            "summary": self._deterministic_summary(selected, operations, grouped_all),
            "operations": [self._operation_json(record) for record in operations],
            "edges": [
                {
                    "from": record.parent_operation_id,
                    "to": record.operation_id,
                    "relation": "contains",
                }
                for record in operations
                if record.parent_operation_id is not None
            ],
            "evidence": evidence,
            "evidence_complete": not missing,
            "missing_evidence": missing,
            "uncertainties": uncertainties,
            "presentation_truncated": truncated,
            "next_cursor": page_rows[-1][1] if truncated and page_rows else None,
        }

    def resolve(
        self,
        kind: str,
        evidence_id: str,
        *,
        evidence_limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        selected_kind = str(kind).strip().lower()
        evidence_types = _RESOLVE_TYPES.get(selected_kind)
        if evidence_types is None:
            raise ValidationError(f"unsupported explain evidence kind: {kind}")
        links = self.store.list_operation_evidence(
            evidence_types=evidence_types,
            evidence_id=str(evidence_id),
        )
        operation_ids = sorted({link.operation_id for link in links})
        if not operation_ids:
            raise NotFound(f"{selected_kind} evidence not found: {evidence_id}")
        roots = sorted(
            {
                record.root_operation_id
                for operation_id in operation_ids
                if (record := self.store.get_operation(operation_id)) is not None
            }
        )
        if len(roots) != 1:
            return {
                "schema_version": self.SCHEMA_VERSION,
                "lookup": {"kind": selected_kind, "id": evidence_id},
                "ambiguous": True,
                "candidates": roots,
            }
        result = self.explain_operation(roots[0], evidence_limit=evidence_limit, cursor=cursor)
        result["lookup"] = {"kind": selected_kind, "id": evidence_id}
        return result

    def _evidence_payload(
        self,
        evidence_type: str,
        evidence_id: str,
        roles: list[str],
        metadata: dict[str, Any],
        linked_at: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "evidence_type": evidence_type,
            "evidence_id": evidence_id,
            "roles": roles,
            "metadata": self._safe(metadata),
        }
        occurred_at: str | None = linked_at
        if evidence_type == "audit":
            record = self.store.get_audit(evidence_id)
            if record is not None:
                occurred_at = record.timestamp
                payload["data"] = {
                    "record_id": record.record_id,
                    "timestamp": record.timestamp,
                    "actor": record.actor,
                    "action": record.action,
                    "target": record.target,
                    "input_refs": list(record.input_refs),
                    "output_refs": list(record.output_refs),
                    "capability_refs": list(record.capability_refs),
                    "decision": self._safe(record.decision),
                    "correlation_id": record.correlation_id,
                    "parent_record_id": record.parent_record_id,
                }
        elif evidence_type == "event":
            event = self.store.get_event(evidence_id)
            if event is not None:
                occurred_at = event.created_at
                payload["data"] = {
                    "event_id": event.event_id,
                    "type": event.type.value,
                    "source": event.source,
                    "target": event.target,
                    "priority": event.priority.value,
                    "created_at": event.created_at,
                    "payload": self._safe(event.payload),
                    "correlation_id": event.correlation_id,
                    "causality": self._safe(event.causality),
                }
        elif evidence_type == "external_effect":
            effect = self.store.get_external_effect(evidence_id)
            if effect is not None:
                occurred_at = effect.created_at
                payload["data"] = {
                    "effect_id": effect.effect_id,
                    "record_id": effect.record_id,
                    "event_id": effect.event_id,
                    "pid": effect.pid,
                    "provider": effect.provider,
                    "operation": effect.operation,
                    "target": effect.target,
                    "rollback_class": effect.rollback_class.value,
                    "rollback_status": effect.rollback_status.value,
                    "state_mutation": effect.state_mutation,
                    "information_flow": effect.information_flow,
                    "effect_state": effect.effect_state,
                    "transaction_state": effect.transaction_state,
                    "canonical_args_hash": effect.canonical_args_hash,
                    "idempotency_key": effect.idempotency_key,
                    "provider_receipt": self._safe(effect.provider_receipt),
                    "updated_at": effect.updated_at,
                    "created_at": effect.created_at,
                    "provider_metadata": self._safe(effect.provider_metadata),
                }
        elif evidence_type == "human_request":
            request = self.store.get_human_request(evidence_id)
            if request is not None:
                occurred_at = request.created_at
                payload["data"] = {
                    "request_id": request.request_id,
                    "pid": request.pid,
                    "human": request.human,
                    "status": request.status.value,
                    "blocking": request.blocking,
                    "created_at": request.created_at,
                    "updated_at": request.updated_at,
                    "content_redacted": True,
                }
        elif evidence_type == "llm_call":
            call = self.store.get_llm_call(evidence_id)
            if call is not None:
                occurred_at = call.created_at
                payload["data"] = {
                    "call_id": call.call_id,
                    "pid": call.pid,
                    "image_id": call.image_id,
                    "purpose": call.purpose,
                    "status": call.status,
                    "api": call.api,
                    "model": call.model,
                    "request_id": call.request_id,
                    "response_id": call.response_id,
                    "usage": dict(call.usage),
                    "observability": self._safe(call.observability),
                    "error_recorded": call.error is not None,
                    "created_at": call.created_at,
                    "completed_at": call.completed_at,
                    "raw_io_redacted": True,
                }
        elif evidence_type == "context_manifest":
            manifest = self.store.get_context_materialization_manifest(evidence_id)
            if manifest is not None:
                occurred_at = manifest.created_at
                payload["data"] = self._manifest_json(manifest)
        elif evidence_type == "capability_reservation":
            reservation = self.store.get_capability_use_reservation(evidence_id)
            if reservation is not None:
                occurred_at = str(reservation.get("created_at") or "")
                payload["data"] = {
                    key: reservation.get(key)
                    for key in (
                        "reservation_id",
                        "cap_id",
                        "count",
                        "status",
                        "reserved_by",
                        "reason",
                        "created_at",
                        "updated_at",
                    )
                }
        if "data" not in payload:
            payload["data"] = {"unavailable": True}
        payload["occurred_at"] = occurred_at
        return payload

    def _deterministic_summary(
        self,
        selected: OperationRecord,
        operations: list[OperationRecord],
        evidence: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        effects = []
        humans = []
        contexts = []
        authorization: list[dict[str, Any]] = []
        resource_consumption: list[dict[str, Any]] = []
        resource_charges = 0
        for (evidence_type, evidence_id), item in evidence.items():
            roles = set(item["roles"])
            if evidence_type == "external_effect":
                effect = self.store.get_external_effect(evidence_id)
                if effect is not None:
                    effects.append(
                        {
                            "effect_id": effect.effect_id,
                            "provider": effect.provider,
                            "operation": effect.operation,
                            "state": effect.effect_state,
                            "transaction_state": effect.transaction_state,
                            "canonical_args_hash": effect.canonical_args_hash,
                            "idempotency_key": effect.idempotency_key,
                            "rollback_class": effect.rollback_class.value,
                            "rollback_status": effect.rollback_status.value,
                        }
                    )
                elif item["metadata"].get("outcome") == "not_started":
                    effects.append(
                        {
                            "effect_id": evidence_id,
                            "provider": item["metadata"].get("provider"),
                            "operation": item["metadata"].get("operation"),
                            "state": "abandoned",
                            "outcome": "not_started",
                            "rollback_class": None,
                            "rollback_status": None,
                        }
                    )
            elif evidence_type == "human_request":
                request = self.store.get_human_request(evidence_id)
                if request is not None:
                    humans.append({"request_id": request.request_id, "status": request.status.value})
            elif evidence_type == "context_manifest":
                manifest = self.store.get_context_materialization_manifest(evidence_id)
                if manifest is not None:
                    contexts.append(
                        {
                            "materialization_id": manifest.materialization_id,
                            "rendered_tokens": manifest.rendered_tokens,
                            "rendered_sha256": manifest.rendered_sha256,
                            "included": sum(item.get("disposition") == "included" for item in manifest.objects),
                            "omitted": sum(item.get("disposition") == "omitted" for item in manifest.objects),
                        }
                    )
            if "resource_charge" in roles:
                resource_charges += 1
                if evidence_type == "audit":
                    charge = self.store.get_audit(evidence_id)
                    decision = charge.decision if charge is not None else None
                    if charge is not None and charge.action == "resource.charge" and isinstance(decision, dict):
                        usage = decision.get("usage")
                        resource_consumption.append(
                            {
                                "audit_id": charge.record_id,
                                "source": decision.get("source"),
                                "usage": dict(usage) if isinstance(usage, dict) else {},
                                "charged_pids": list(decision.get("charged_pids") or []),
                            }
                        )
            if evidence_type == "audit" and "decision" in roles:
                audit = self.store.get_audit(evidence_id)
                decision = audit.decision if audit is not None else None
                if isinstance(decision, dict):
                    authorization.append(
                        {
                            "allowed": decision.get("allowed"),
                            "reason": decision.get("reason"),
                            "right": decision.get("right"),
                            "resource": decision.get("resource"),
                            "selected_capability_id": decision.get("selected_capability_id"),
                        }
                    )
        outcome = selected.outcome.value
        headline = {
            OperationOutcome.SUCCEEDED.value: f"{selected.name} completed successfully.",
            OperationOutcome.DENIED.value: f"{selected.name} was denied before completion.",
            OperationOutcome.FAILED.value: f"{selected.name} failed.",
            OperationOutcome.INTERRUPTED.value: f"{selected.name} was interrupted.",
            OperationOutcome.UNKNOWN.value: f"{selected.name} has an unknown external outcome.",
            OperationOutcome.PENDING.value: f"{selected.name} is still {selected.state.value}.",
        }[outcome]
        authority = None
        manifests = getattr(self.runtime, "authority_manifests", None)
        if manifests is not None and selected.pid is not None:
            authority = manifests.summary_for_process(selected.pid)
        return {
            "headline": headline,
            "outcome": outcome,
            "operation_count": len(operations),
            "authorization": authorization,
            "human": humans,
            "external_effects": effects,
            "resource_charge_evidence_count": resource_charges,
            "resource_charge_count": len(resource_consumption),
            "resource_consumption": resource_consumption,
            "context": contexts,
            "authority_manifest": self._safe(authority),
        }

    @staticmethod
    def _group_links(links: Iterable[Any]) -> dict[tuple[str, str], dict[str, Any]]:
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for link in links:
            key = (link.evidence_type, link.evidence_id)
            item = grouped.setdefault(
                key,
                {
                    "roles": [],
                    "metadata": {},
                    "occurred_at": link.created_at,
                    "cursor": link.link_id,
                    "link_ids": [],
                },
            )
            item["link_ids"].append(link.link_id)
            if link.role not in item["roles"]:
                item["roles"].append(link.role)
            item["metadata"].update(link.metadata)
        for item in grouped.values():
            item["roles"].sort()
        return grouped

    @staticmethod
    def _missing_evidence(operations: list[OperationRecord], links: Iterable[Any]) -> list[dict[str, str]]:
        roles_by_operation: dict[str, set[str]] = defaultdict(set)
        for link in links:
            roles_by_operation[link.operation_id].add(link.role)
        missing: list[dict[str, str]] = []
        for operation in operations:
            available = roles_by_operation[operation.operation_id]
            for role in operation.expected_roles:
                if role not in available:
                    missing.append({"operation_id": operation.operation_id, "role": role})
        return missing

    def _uncertainties(
        self,
        operations: list[OperationRecord],
        evidence: dict[tuple[str, str], dict[str, Any]],
    ) -> list[dict[str, str]]:
        uncertainties: list[dict[str, str]] = []
        for operation in operations:
            if operation.state.value == "waiting":
                uncertainties.append({"operation_id": operation.operation_id, "reason": "operation_waiting"})
            elif operation.outcome == OperationOutcome.INTERRUPTED:
                uncertainties.append({"operation_id": operation.operation_id, "reason": "operation_interrupted"})
            elif operation.outcome == OperationOutcome.UNKNOWN:
                uncertainties.append({"operation_id": operation.operation_id, "reason": "operation_outcome_unknown"})
        for evidence_type, evidence_id in evidence:
            if evidence_type != "external_effect":
                continue
            effect = self.store.get_external_effect(evidence_id)
            if effect is not None and effect.effect_state == "pending":
                uncertainties.append({"evidence_id": evidence_id, "reason": "provider_outcome_unknown"})
        return uncertainties

    @staticmethod
    def _root_operation(operations: list[OperationRecord], root_id: str) -> OperationRecord:
        return next(record for record in operations if record.operation_id == root_id)

    def _operation_json(self, record: OperationRecord) -> dict[str, Any]:
        payload = asdict(record)
        payload["kind"] = record.kind.value
        payload["state"] = record.state.value
        payload["outcome"] = record.outcome.value
        payload["metadata"] = self._safe(record.metadata)
        return payload

    def _operation_summary(self, record: OperationRecord) -> dict[str, Any]:
        return {
            "operation_id": record.operation_id,
            "root_operation_id": record.root_operation_id,
            "parent_operation_id": record.parent_operation_id,
            "kind": record.kind.value,
            "name": record.name,
            "actor": record.actor,
            "pid": record.pid,
            "state": record.state.value,
            "outcome": record.outcome.value,
            "started_at": record.started_at,
            "updated_at": record.updated_at,
            "completed_at": record.completed_at,
        }

    @staticmethod
    def _manifest_json(manifest: ContextMaterializationManifest) -> dict[str, Any]:
        object_fields = (
            "oid",
            "version",
            "type",
            "disposition",
            "reason",
            "transform",
            "tokens",
            "rendered_sha256",
            "labels",
        )
        return {
            "materialization_id": manifest.materialization_id,
            "pid": manifest.pid,
            "view_id": manifest.view_id,
            "policy": manifest.policy,
            "budget_tokens": manifest.budget_tokens,
            "rendered_tokens": manifest.rendered_tokens,
            "rendered_sha256": manifest.rendered_sha256,
            "context_generation": manifest.context_generation,
            "context_oid": manifest.context_oid,
            "context_version": manifest.context_version,
            "objects": [
                {key: item.get(key) for key in object_fields}
                for item in manifest.objects
                if isinstance(item, dict)
            ],
            "compaction": {
                key: manifest.compaction.get(key)
                for key in ("mode", "compacted_at", "transform")
            },
            "created_at": manifest.created_at,
            "content_redacted": True,
        }

    @staticmethod
    def _safe(value: Any) -> Any:
        if value is None:
            return None
        return sanitize_for_observability(
            value,
            sensitive_keys=_EXPLAIN_SENSITIVE_KEYS,
        )

    @staticmethod
    def _bounded_limit(value: int | None, default: int, maximum: int) -> int:
        selected = default if value is None else int(value)
        if selected < 1:
            raise ValidationError("explain limit must be >= 1")
        return min(selected, maximum)

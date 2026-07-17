from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from agent_libos.capability.lease import CapabilityLeaseService
from agent_libos.models import (
    Capability,
    CapabilityDecision,
    CapabilityEffect,
    CapabilityStatus,
    EventType,
)
from agent_libos.models.exceptions import CapabilityDenied, NotFound
from agent_libos.ports import AuditPort, CapabilityStorePort, EventPort
from agent_libos.utils.ids import new_id, utc_now


@dataclass(frozen=True, slots=True)
class CapabilityDraft:
    subject: str
    resource: str
    rights: set[str]
    effect: CapabilityEffect
    constraints: dict[str, Any]
    metadata: dict[str, Any]
    issued_by: str
    issuer_cap_id: str | None
    parent_cap_id: str | None
    delegation_depth: int
    max_delegation_depth: int | None
    expires_at: str | None
    uses_remaining: int | None
    delegable: bool
    revocable: bool


class CapabilityMutationService:
    """Durable issue, delegation publication, disable, and revoke mutations."""

    def __init__(
        self,
        store: CapabilityStorePort,
        audit: AuditPort,
        events: EventPort,
        leases: CapabilityLeaseService,
    ) -> None:
        self.store = store
        self.audit = audit
        self.events = events
        self.leases = leases

    def issue(
        self,
        draft: CapabilityDraft,
        *,
        actor: str,
        authority_decision: CapabilityDecision | None,
        attach_to_process: Callable[[str, str], None],
    ) -> Capability:
        with self.store.transaction():
            reservation = self.leases.reserve_decision(
                authority_decision,
                used_by=actor,
                reason="one-time issue authority reserved",
            )
            cap = self.publish(draft, attach_to_process=attach_to_process)
            self.leases.commit(
                reservation,
                committed_by=actor,
                reason="one-time issue authority committed",
            )
            self.audit.record(
                actor=actor,
                action="capability.issue",
                target=f"{draft.subject}:{cap.resource}",
                capability_refs=[cap.cap_id] + ([draft.issuer_cap_id] if draft.issuer_cap_id else []),
                decision={
                    "effect": cap.effect.value,
                    "rights": sorted(cap.rights),
                    "uses_remaining": cap.uses_remaining,
                    "delegable": cap.delegable,
                },
            )
        return cap

    def publish(
        self,
        draft: CapabilityDraft,
        *,
        attach_to_process: Callable[[str, str], None],
    ) -> Capability:
        cap = Capability(
            cap_id=new_id("cap"),
            subject=draft.subject,
            resource=draft.resource,
            rights=set(draft.rights),
            constraints=dict(draft.constraints),
            issued_by=draft.issued_by,
            issued_at=utc_now(),
            expires_at=draft.expires_at,
            delegable=draft.delegable,
            revocable=draft.revocable,
            effect=draft.effect,
            issuer_cap_id=draft.issuer_cap_id,
            parent_cap_id=draft.parent_cap_id,
            delegation_depth=draft.delegation_depth,
            max_delegation_depth=draft.max_delegation_depth,
            uses_remaining=draft.uses_remaining,
            status=CapabilityStatus.ACTIVE,
            metadata=dict(draft.metadata),
        )
        self.store.insert_capability(cap)
        attach_to_process(cap.subject, cap.cap_id)
        self.events.emit(
            EventType.CAPABILITY_GRANTED,
            source=cap.issued_by,
            target=cap.subject,
            payload={
                "capability_id": cap.cap_id,
                "resource": cap.resource,
                "rights": sorted(cap.rights),
                "effect": cap.effect.value,
                "uses_remaining": cap.uses_remaining,
            },
        )
        return cap

    def record_delegation(
        self,
        cap: Capability,
        *,
        parent_cap: Capability,
        parent_subject: str,
        child_subject: str,
        actor: str,
    ) -> None:
        self.audit.record(
            actor=actor,
            action="capability.delegate",
            target=f"{parent_subject}->{child_subject}:{cap.resource}",
            capability_refs=[parent_cap.cap_id, cap.cap_id],
            decision={"rights": sorted(cap.rights), "effect": cap.effect.value},
        )

    def revoke(
        self,
        cap_id: str,
        *,
        revoked_by: str,
        reason: str | None,
        authority_decision: CapabilityDecision | None,
    ) -> Capability:
        with self.store.transaction():
            current = self._require_revocable(cap_id)
            reservation = self.leases.reserve_decision(
                authority_decision,
                used_by=revoked_by,
                reason="one-time revoke authority reserved",
            )
            revoked = replace(current, status=CapabilityStatus.REVOKED)
            self.store.update_capability(revoked)
            self.leases.commit(
                reservation,
                committed_by=revoked_by,
                reason="one-time revoke authority committed",
            )
            self.events.emit(
                EventType.CAPABILITY_REVOKED,
                source=revoked_by,
                target=current.subject,
                payload={"capability_id": cap_id, "reason": reason},
            )
            self.audit.record(
                actor=revoked_by,
                action="capability.revoke",
                target=current.resource,
                capability_refs=[cap_id],
                decision={"revoked": True, "reason": reason, "subject": current.subject},
            )
        return revoked

    def disable(self, cap_id: str, *, actor: str, reason: str | None = None) -> Capability:
        cap = self._require(cap_id)
        updated = replace(cap, status=CapabilityStatus.DISABLED)
        self.store.update_capability(updated)
        self.audit.record(
            actor=actor,
            action="capability.disable",
            target=cap.resource,
            capability_refs=[cap_id],
            decision={"reason": reason, "subject": cap.subject},
        )
        return updated

    def revoke_resource(
        self,
        resource: str,
        *,
        revoked_by: str,
        reason: str | None = None,
    ) -> list[Capability]:
        revoked: list[Capability] = []
        for cap in self.store.list_capabilities():
            if cap.resource != resource or not cap.active:
                continue
            updated = replace(cap, status=CapabilityStatus.REVOKED)
            self.store.update_capability(updated)
            revoked.append(updated)
            self.events.emit(
                EventType.CAPABILITY_REVOKED,
                source=revoked_by,
                target=cap.subject,
                payload={"capability_id": cap.cap_id, "reason": reason},
            )
        if revoked:
            self.audit.record(
                actor=revoked_by,
                action="capability.revoke_resource",
                target=resource,
                capability_refs=[cap.cap_id for cap in revoked],
                decision={"revoked": len(revoked), "reason": reason},
            )
        return revoked

    def _require(self, cap_id: str) -> Capability:
        cap = self.store.get_capability(cap_id)
        if cap is None:
            raise NotFound(f"capability not found: {cap_id}")
        return cap

    def _require_revocable(self, cap_id: str) -> Capability:
        cap = self._require(cap_id)
        if not cap.revocable:
            raise CapabilityDenied(f"capability is not revocable: {cap_id}")
        return cap

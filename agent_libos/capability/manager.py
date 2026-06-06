from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Iterable

from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models import (
    Capability,
    CapabilityDecision,
    CapabilityEffect,
    CapabilityRight,
    CapabilitySpec,
    CapabilityStatus,
    EventType,
    ObjectHandle,
    OperationContext,
    ResourcePattern,
    ResourceScope,
)
from agent_libos.models.exceptions import CapabilityDenied, NotFound, ValidationError
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.storage import SQLiteStore
from agent_libos.utils.ids import new_id, utc_now


class CapabilityManager:
    """Capability v2 directory, authorization engine, and delegation helper."""

    POLICY_KEY = "permission_policy"
    ALWAYS_ALLOW = "always_allow"
    ALWAYS_DENY = "always_deny"
    ASK_EACH_TIME = "ask_each_time"
    ALLOW_ONCE = "allow_once"
    MISSING = "missing"
    POLICY_VALUES = {ALWAYS_ALLOW, ALWAYS_DENY, ASK_EACH_TIME, ALLOW_ONCE}

    _KNOWN_CONSTRAINT_KEYS = {
        POLICY_KEY,
        "shell_policy_level",
        "inherited_from",
    }

    def __init__(self, store: SQLiteStore, audit: AuditManager, events: EventBus, config: AgentLibOSConfig | None = None):
        self.config = config or DEFAULT_CONFIG
        self.store = store
        self.audit = audit
        self.events = events

    def issue(
        self,
        actor: str,
        subject: str,
        spec: CapabilitySpec | dict[str, Any],
        *,
        issuer_cap_id: str | None = None,
        require_authority: bool = True,
    ) -> Capability:
        selected = self._coerce_spec(spec)
        if require_authority:
            issuer_cap_id = self._require_issue_authority(actor, selected)
        cap = self._insert_capability(
            subject=subject,
            resource=selected.resource,
            rights=selected.rights,
            effect=selected.effect,
            constraints=selected.constraints,
            metadata=selected.metadata,
            issued_by=actor,
            issuer_cap_id=issuer_cap_id,
            parent_cap_id=None,
            delegation_depth=0,
            expires_at=selected.expires_at,
            uses_remaining=selected.uses_remaining,
            delegable=selected.delegable,
            revocable=selected.revocable,
        )
        self.audit.record(
            actor=actor,
            action="capability.issue",
            target=f"{subject}:{cap.resource}",
            capability_refs=[cap.cap_id] + ([issuer_cap_id] if issuer_cap_id else []),
            decision={
                "effect": cap.effect.value,
                "rights": sorted(cap.rights),
                "uses_remaining": cap.uses_remaining,
                "delegable": cap.delegable,
            },
        )
        return cap

    def issue_trusted(
        self,
        subject: str,
        resource: str,
        rights: Iterable[str | CapabilityRight],
        *,
        issued_by: str,
        effect: str | CapabilityEffect = CapabilityEffect.ALLOW,
        constraints: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        expires_at: str | None = None,
        uses_remaining: int | None = None,
        delegable: bool = False,
        revocable: bool = True,
    ) -> Capability:
        return self.issue(
            actor=issued_by,
            subject=subject,
            spec=CapabilitySpec(
                resource=resource,
                rights=self._normalize_rights(rights),
                effect=CapabilityEffect(effect),
                constraints=dict(constraints or {}),
                metadata=dict(metadata or {}),
                expires_at=expires_at,
                uses_remaining=uses_remaining,
                delegable=delegable,
                revocable=revocable,
            ),
            require_authority=False,
        )

    def grant(
        self,
        subject: str,
        resource: str,
        rights: Iterable[str | CapabilityRight],
        issued_by: str = "system",
        constraints: dict | None = None,
        expires_at: str | None = None,
        delegable: bool = False,
        revocable: bool = True,
    ) -> Capability:
        # Compatibility wrapper for runtime bootstrap and tests. New actor-facing
        # code should call issue(..., require_authority=True).
        effect, uses_remaining = self._effect_from_legacy_constraints(constraints or {})
        clean_constraints = {
            key: value
            for key, value in dict(constraints or {}).items()
            if key != self.POLICY_KEY
        }
        return self.issue_trusted(
            subject=subject,
            resource=resource,
            rights=rights,
            issued_by=issued_by,
            effect=effect,
            constraints=clean_constraints,
            expires_at=expires_at,
            uses_remaining=uses_remaining,
            delegable=delegable,
            revocable=revocable,
        )

    def delegate(
        self,
        parent: str,
        child: str,
        spec: CapabilitySpec | dict[str, Any],
        *,
        actor: str | None = None,
    ) -> Capability:
        selected = self._coerce_spec(spec)
        parent_cap = self._find_delegation_parent(parent, selected)
        self._validate_delegation_parent(parent_cap, selected)
        cap = self._insert_capability(
            subject=child,
            resource=selected.resource,
            rights=selected.rights,
            effect=selected.effect,
            constraints=selected.constraints,
            metadata={**selected.metadata, "delegated_from": parent},
            issued_by=actor or parent,
            issuer_cap_id=parent_cap.cap_id,
            parent_cap_id=parent_cap.cap_id,
            delegation_depth=parent_cap.delegation_depth + 1,
            expires_at=selected.expires_at or parent_cap.expires_at,
            uses_remaining=selected.uses_remaining,
            delegable=selected.delegable,
            revocable=selected.revocable,
        )
        self.audit.record(
            actor=actor or parent,
            action="capability.delegate",
            target=f"{parent}->{child}:{cap.resource}",
            capability_refs=[parent_cap.cap_id, cap.cap_id],
            decision={"rights": sorted(cap.rights), "effect": cap.effect.value},
        )
        return cap

    def validate_delegation(self, parent: str, spec: CapabilitySpec | dict[str, Any]) -> Capability:
        selected = self._coerce_spec(spec)
        parent_cap = self._find_delegation_parent(parent, selected)
        self._validate_delegation_parent(parent_cap, selected)
        return parent_cap

    def inherit(
        self,
        parent: str,
        child: str,
        resource: str,
        rights: Iterable[str | CapabilityRight],
        issued_by: str,
        constraints: dict | None = None,
    ) -> Capability:
        return self.delegate(
            parent,
            child,
            CapabilitySpec(
                resource=resource,
                rights=self._normalize_rights(rights),
                effect=CapabilityEffect.ALLOW,
                constraints=dict(constraints or {}),
                metadata={"issued_by": issued_by},
                delegable=False,
            ),
            actor=issued_by,
        )

    def authorize(
        self,
        subject: str,
        resource: str,
        right: str | CapabilityRight,
        context: OperationContext | dict[str, Any] | None = None,
        *,
        audit: bool = False,
    ) -> CapabilityDecision:
        requested_right = str(right)
        selected_context = self._context_dict(context)
        matches = self._matching_capabilities(subject, resource, requested_right, include_ask=True)
        matched_ids = [cap.cap_id for cap in matches]
        deny = next((cap for cap in matches if cap.effect == CapabilityEffect.DENY), None)
        if deny is not None:
            decision = CapabilityDecision(
                subject=subject,
                resource=resource,
                right=requested_right,
                allowed=False,
                effect=CapabilityEffect.DENY,
                reason=f"{subject} denied {requested_right} on {resource}",
                matched_capability_ids=matched_ids,
                selected_capability_id=deny.cap_id,
                issuer_chain=self._issuer_chain(deny),
                context=selected_context,
            )
            return self._record_decision(decision, audit=audit)
        for cap in matches:
            constraint_results = self._evaluate_constraints(cap, selected_context)
            if not all(bool(item.get("ok")) for item in constraint_results.values()):
                decision = CapabilityDecision(
                    subject=subject,
                    resource=resource,
                    right=requested_right,
                    allowed=False,
                    effect=cap.effect,
                    reason=f"capability constraints rejected {requested_right} on {resource}",
                    matched_capability_ids=matched_ids,
                    selected_capability_id=cap.cap_id,
                    issuer_chain=self._issuer_chain(cap),
                    constraint_results=constraint_results,
                    context=selected_context,
                )
                return self._record_decision(decision, audit=audit)
            if cap.effect == CapabilityEffect.ASK:
                decision = CapabilityDecision(
                    subject=subject,
                    resource=resource,
                    right=requested_right,
                    allowed=False,
                    effect=CapabilityEffect.ASK,
                    reason=f"{subject} requires human approval for {requested_right} on {resource}",
                    matched_capability_ids=matched_ids,
                    selected_capability_id=cap.cap_id,
                    issuer_chain=self._issuer_chain(cap),
                    constraint_results=constraint_results,
                    context=selected_context,
                )
                return self._record_decision(decision, audit=audit)
            if cap.effect == CapabilityEffect.ALLOW:
                decision = CapabilityDecision(
                    subject=subject,
                    resource=resource,
                    right=requested_right,
                    allowed=True,
                    effect=CapabilityEffect.ALLOW,
                    reason="capability allowed operation",
                    matched_capability_ids=matched_ids,
                    selected_capability_id=cap.cap_id,
                    consume_capability_id=cap.cap_id if cap.uses_remaining is not None else None,
                    issuer_chain=self._issuer_chain(cap),
                    constraint_results=constraint_results,
                    context=selected_context,
                )
                return self._record_decision(decision, audit=audit)
        decision = CapabilityDecision(
            subject=subject,
            resource=resource,
            right=requested_right,
            allowed=False,
            effect=None,
            reason=f"{subject} lacks {requested_right} on {resource}",
            matched_capability_ids=matched_ids,
            context=selected_context,
        )
        return self._record_decision(decision, audit=audit)

    def require(
        self,
        subject: str,
        resource: str,
        right: str | CapabilityRight,
        context: OperationContext | dict[str, Any] | None = None,
    ) -> CapabilityDecision:
        decision = self.authorize(subject, resource, right, context, audit=True)
        if decision.allowed:
            return decision
        raise CapabilityDenied(decision.reason)

    def check(
        self,
        subject: str,
        resource: str,
        right: str | CapabilityRight,
        context: OperationContext | dict[str, Any] | None = None,
    ) -> bool:
        return self.authorize(subject, resource, right, context).allowed

    def permission_policy(
        self,
        subject: str,
        resource: str,
        right: str | CapabilityRight,
        context: OperationContext | dict[str, Any] | None = None,
    ) -> str:
        return self.authorize(subject, resource, right, context).policy

    def set_permission_policy(
        self,
        subject: str,
        resource: str,
        rights: Iterable[str | CapabilityRight],
        policy: str,
        issued_by: str | None = None,
        constraints: dict | None = None,
    ) -> Capability:
        if policy not in self.POLICY_VALUES:
            raise ValueError(f"unknown permission policy: {policy}")
        effect, uses_remaining = self._effect_from_policy(policy)
        cap = self.issue_trusted(
            subject=subject,
            resource=resource,
            rights=rights,
            issued_by=issued_by or self.config.runtime.default_human_actor,
            effect=effect,
            constraints=dict(constraints or {}),
            uses_remaining=uses_remaining,
        )
        self.audit.record(
            actor=issued_by or self.config.runtime.default_human_actor,
            action="capability.permission_policy",
            target=f"{subject}:{resource}",
            capability_refs=[cap.cap_id],
            decision={"policy": policy, "effect": effect.value, "rights": sorted(cap.rights)},
        )
        return cap

    def grant_once(
        self,
        subject: str,
        resource: str,
        rights: Iterable[str | CapabilityRight],
        issued_by: str | None = None,
        constraints: dict | None = None,
    ) -> Capability:
        return self.issue_trusted(
            subject=subject,
            resource=resource,
            rights=rights,
            issued_by=issued_by or self.config.runtime.default_human_actor,
            effect=CapabilityEffect.ALLOW,
            constraints=dict(constraints or {}),
            uses_remaining=1,
        )

    def consume_use(self, cap_id: str, *, used_by: str, reason: str = "capability use consumed") -> Capability:
        cap = self.store.get_capability(cap_id)
        if cap is None:
            raise NotFound(f"capability not found: {cap_id}")
        if cap.uses_remaining is None:
            return cap
        remaining = max(0, cap.uses_remaining - 1)
        updated = replace(
            cap,
            uses_remaining=remaining,
            status=CapabilityStatus.REVOKED if remaining == 0 else cap.status,
        )
        self.store.update_capability(updated)
        self.audit.record(
            actor=used_by,
            action="capability.consume",
            target=cap.resource,
            capability_refs=[cap_id],
            decision={"uses_remaining": remaining, "reason": reason},
        )
        if updated.revoked:
            self.events.emit(
                EventType.CAPABILITY_REVOKED,
                source=used_by,
                target=cap.subject,
                payload={"capability_id": cap_id, "reason": reason},
            )
        return updated

    def consume_allow_once(self, subject: str, resource: str, right: str | CapabilityRight, used_by: str) -> None:
        decision = self.authorize(subject, resource, right)
        if decision.consume_capability_id is not None:
            self.consume_use(decision.consume_capability_id, used_by=used_by, reason="one-time permission consumed")

    def revoke(
        self,
        cap_id: str,
        revoked_by: str = "system",
        reason: str | None = None,
        *,
        require_authority: bool = True,
    ) -> Capability:
        cap = self.store.get_capability(cap_id)
        if cap is None:
            raise NotFound(f"capability not found: {cap_id}")
        if not cap.revocable:
            raise CapabilityDenied(f"capability is not revocable: {cap_id}")
        if require_authority:
            self._require_revoke_authority(revoked_by, cap)
        revoked = replace(cap, status=CapabilityStatus.REVOKED)
        self.store.update_capability(revoked)
        self.events.emit(
            EventType.CAPABILITY_REVOKED,
            source=revoked_by,
            target=cap.subject,
            payload={"capability_id": cap_id, "reason": reason},
        )
        self.audit.record(
            actor=revoked_by,
            action="capability.revoke",
            target=cap.resource,
            capability_refs=[cap_id],
            decision={"revoked": True, "reason": reason, "subject": cap.subject},
        )
        return revoked

    def disable_subject_capability(
        self,
        cap_id: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> Capability:
        cap = self.store.get_capability(cap_id)
        if cap is None:
            raise NotFound(f"capability not found: {cap_id}")
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

    def assert_handle(self, subject: str, handle: ObjectHandle, right: str | CapabilityRight) -> None:
        requested = str(right)
        if requested not in handle.rights:
            raise CapabilityDenied(f"object handle lacks {requested}: {handle.oid}")
        cap = self.store.get_capability(handle.capability_id)
        if cap is None or cap.revoked or not cap.active:
            raise CapabilityDenied(f"invalid object capability: {handle.capability_id}")
        if cap.subject != subject:
            raise CapabilityDenied(f"capability subject mismatch: {cap.subject} != {subject}")
        decision = self.authorize(subject, f"object:{handle.oid}", requested)
        if not decision.allowed:
            raise CapabilityDenied(f"capability lacks {requested}: {handle.oid}")

    def handle_for_object(
        self,
        subject: str,
        oid: str,
        rights: Iterable[str | CapabilityRight],
        issued_by: str = "system",
        expires_at: str | None = None,
    ) -> ObjectHandle:
        normalized = self._normalize_rights(rights)
        cap = self.issue_trusted(
            subject=subject,
            resource=f"object:{oid}",
            rights=normalized,
            issued_by=issued_by,
            expires_at=expires_at,
            delegable=False,
        )
        return ObjectHandle(oid=oid, rights=normalized, capability_id=cap.cap_id, expires_at=expires_at)

    def capabilities_for(self, subject: str) -> list[Capability]:
        return self.store.list_capabilities(subject=subject)

    def list_subject(self, subject: str, *, include_inactive: bool = False, limit: int | None = None) -> list[Capability]:
        caps = self.capabilities_for(subject)
        if not include_inactive:
            caps = [cap for cap in caps if cap.active and not self._is_expired(cap)]
        return caps[: (limit or self.config.capability.list_limit)]

    def inspect(self, cap_id: str) -> dict[str, Any]:
        cap = self.store.get_capability(cap_id)
        if cap is None:
            raise NotFound(f"capability not found: {cap_id}")
        return self._capability_json(cap)

    def explain_decision(
        self,
        subject: str,
        resource: str,
        right: str | CapabilityRight,
        context: OperationContext | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        decision = self.authorize(subject, resource, right, context)
        return self._decision_json(decision)

    def spec(
        self,
        resource: str,
        rights: Iterable[str | CapabilityRight],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {"resource": resource, "rights": [str(right) for right in rights], **kwargs}

    def tool_execute(self, tool: str, rights: Iterable[str | CapabilityRight] | None = None, **kwargs: Any) -> dict[str, Any]:
        resource = tool if tool.startswith("tool:") else f"tool:{tool}"
        return self.spec(resource, rights or [CapabilityRight.EXECUTE], **kwargs)

    def project_read(self, name: str, **kwargs: Any) -> dict[str, Any]:
        return self.spec(f"project:{name}", [CapabilityRight.READ], **kwargs)

    def object_access(self, oid: str, rights: Iterable[str | CapabilityRight], **kwargs: Any) -> dict[str, Any]:
        return self.spec(f"object:{oid}", rights, **kwargs)

    def parse_resource_pattern(self, resource: str, *, requested: bool = False) -> ResourcePattern:
        raw = self._canonical_resource(resource)
        if raw == "*":
            raise CapabilityDenied("capability resources must be typed; use '<kind>:*' instead of global '*'")
        if "*" in raw and not (raw.endswith(":*") or raw.endswith("/*")):
            raise CapabilityDenied(f"resource wildcard must be terminal and canonical: {resource}")
        if ":" not in raw:
            raise CapabilityDenied(f"resource must be typed: {resource}")
        kind, body = raw.split(":", 1)
        if not kind or not body:
            raise CapabilityDenied(f"resource must be typed: {resource}")
        if raw.endswith(":*"):
            if requested and raw != resource:
                raise CapabilityDenied(f"requested resource is not canonical: {resource}")
            return ResourcePattern(raw=raw, kind=kind, body=body[:-2], scope=ResourceScope.PREFIX)
        if raw.endswith("/*"):
            return ResourcePattern(raw=raw, kind=kind, body=body[:-2].rstrip("/"), scope=ResourceScope.SUBTREE)
        return ResourcePattern(raw=raw, kind=kind, body=body, scope=ResourceScope.EXACT)

    def _insert_capability(
        self,
        *,
        subject: str,
        resource: str,
        rights: set[str],
        effect: CapabilityEffect,
        constraints: dict[str, Any],
        metadata: dict[str, Any],
        issued_by: str,
        issuer_cap_id: str | None,
        parent_cap_id: str | None,
        delegation_depth: int,
        expires_at: str | None,
        uses_remaining: int | None,
        delegable: bool,
        revocable: bool,
    ) -> Capability:
        if not subject:
            raise ValidationError("capability subject must be non-empty")
        self.parse_resource_pattern(resource)
        normalized_rights = self._normalize_rights(rights)
        self._validate_constraints(constraints)
        if uses_remaining is not None and uses_remaining < 1:
            raise ValidationError("uses_remaining must be >= 1 when set")
        cap = Capability(
            cap_id=new_id("cap"),
            subject=subject,
            resource=self._canonical_resource(resource),
            rights=normalized_rights,
            constraints=dict(constraints),
            issued_by=issued_by,
            issued_at=utc_now(),
            expires_at=expires_at,
            delegable=delegable,
            revocable=revocable,
            effect=effect,
            issuer_cap_id=issuer_cap_id,
            parent_cap_id=parent_cap_id,
            delegation_depth=delegation_depth,
            uses_remaining=uses_remaining,
            status=CapabilityStatus.ACTIVE,
            metadata=dict(metadata),
        )
        self.store.insert_capability(cap)
        self._attach_to_process(subject, cap.cap_id)
        self.events.emit(
            EventType.CAPABILITY_GRANTED,
            source=issued_by,
            target=subject,
            payload={
                "capability_id": cap.cap_id,
                "resource": cap.resource,
                "rights": sorted(cap.rights),
                "effect": cap.effect.value,
                "uses_remaining": cap.uses_remaining,
            },
        )
        return cap

    def _require_issue_authority(self, actor: str, spec: CapabilitySpec) -> str | None:
        if self._is_trusted_issuer(actor):
            return None
        for right in spec.rights:
            grant = self.authorize(actor, spec.resource, CapabilityRight.GRANT)
            if grant.allowed:
                continue
            admin = self.authorize(actor, spec.resource, CapabilityRight.ADMIN)
            if admin.allowed:
                continue
            raise CapabilityDenied(f"{actor} lacks grant/admin authority to issue {right} on {spec.resource}")
        grant = self.authorize(actor, spec.resource, CapabilityRight.GRANT)
        return grant.selected_capability_id if grant.allowed else None

    def _require_revoke_authority(self, actor: str, cap: Capability) -> None:
        if self._is_trusted_issuer(actor) or actor == cap.issued_by:
            return
        if actor == cap.subject:
            if cap.effect == CapabilityEffect.ALLOW:
                return
            raise CapabilityDenied(f"{actor} cannot self-revoke restrictive capability {cap.cap_id}")
        revoke = self.authorize(actor, cap.resource, CapabilityRight.REVOKE)
        if revoke.allowed:
            return
        admin = self.authorize(actor, cap.resource, CapabilityRight.ADMIN)
        if admin.allowed:
            return
        raise CapabilityDenied(f"{actor} lacks revoke/admin authority for capability {cap.cap_id}")

    def _find_delegation_parent(self, parent: str, spec: CapabilitySpec) -> Capability:
        candidates = [
            cap
            for cap in self.capabilities_for(parent)
            if cap.active
            and not self._is_expired(cap)
            and cap.effect == CapabilityEffect.ALLOW
            and cap.delegable
            and self._resource_covers(cap.resource, spec.resource)
            and spec.rights.issubset(cap.rights)
        ]
        if not candidates:
            raise CapabilityDenied(f"{parent} cannot delegate {sorted(spec.rights)} on {spec.resource}")
        candidates.sort(key=lambda cap: (len(cap.resource), cap.issued_at), reverse=True)
        return candidates[0]

    def _validate_delegation_parent(self, parent_cap: Capability, selected: CapabilitySpec) -> None:
        if selected.delegable and not parent_cap.delegable:
            raise CapabilityDenied(f"parent capability is not delegable: {parent_cap.cap_id}")
        if parent_cap.expires_at is not None and selected.expires_at is not None and selected.expires_at > parent_cap.expires_at:
            raise CapabilityDenied("delegated capability cannot outlive parent capability")
        if parent_cap.uses_remaining is not None:
            if selected.uses_remaining is None or selected.uses_remaining > parent_cap.uses_remaining:
                raise CapabilityDenied("delegated capability cannot have broader use count than parent")
        max_depth = selected.max_delegation_depth or self.config.capability.default_delegation_depth
        if parent_cap.delegation_depth >= max_depth:
            raise CapabilityDenied("delegation depth exhausted")
        self._require_constraint_attenuation(parent_cap, selected)

    def _matching_capabilities(
        self,
        subject: str,
        resource: str,
        right: str | CapabilityRight,
        *,
        include_ask: bool = False,
    ) -> list[Capability]:
        requested_right = str(right)
        self.parse_resource_pattern(resource, requested=True)
        matches: list[Capability] = []
        for cap in self.store.list_capabilities(subject=subject):
            if not cap.active or self._is_expired(cap):
                continue
            if not include_ask and cap.effect == CapabilityEffect.ASK:
                continue
            if not self._resource_matches(cap.resource, resource):
                continue
            if requested_right not in cap.rights:
                continue
            matches.append(cap)
        matches.sort(
            key=lambda cap: (
                0 if cap.effect == CapabilityEffect.DENY else 1,
                len(cap.resource),
                cap.issued_at,
            ),
            reverse=True,
        )
        # Deny dominates regardless of specificity.
        matches.sort(key=lambda cap: 0 if cap.effect == CapabilityEffect.DENY else 1)
        return matches

    def _resource_matches(self, granted: str, requested: str) -> bool:
        try:
            pattern = self.parse_resource_pattern(granted)
            request = self.parse_resource_pattern(requested, requested=True)
        except CapabilityDenied:
            return False
        if pattern.scope == ResourceScope.GLOBAL:
            return True
        if pattern.kind != request.kind:
            return False
        if pattern.scope == ResourceScope.EXACT:
            return pattern.raw == request.raw
        if pattern.scope == ResourceScope.PREFIX:
            if not pattern.body:
                return request.raw.startswith(f"{pattern.kind}:")
            return request.raw.startswith(f"{pattern.kind}:{pattern.body}:") or request.raw == f"{pattern.kind}:{pattern.body}"
        if pattern.scope == ResourceScope.SUBTREE:
            body = request.body
            return body == pattern.body or body.startswith(f"{pattern.body}/")
        return False

    def _resource_covers(self, granted: str, requested_pattern: str) -> bool:
        if granted == requested_pattern:
            return True
        try:
            requested = self.parse_resource_pattern(requested_pattern)
        except CapabilityDenied:
            return False
        if requested.scope in {ResourceScope.PREFIX, ResourceScope.SUBTREE}:
            return self._resource_matches(granted, requested.raw)
        return self._resource_matches(granted, requested_pattern)

    def _coerce_spec(self, spec: CapabilitySpec | dict[str, Any]) -> CapabilitySpec:
        if isinstance(spec, CapabilitySpec):
            return spec
        if not isinstance(spec, dict):
            raise ValidationError("capability spec must be a mapping")
        effect = spec.get("effect", CapabilityEffect.ALLOW)
        policy = spec.get("policy")
        uses_remaining = spec.get("uses_remaining")
        if policy is not None:
            effect, uses_remaining = self._effect_from_policy(str(policy))
        return CapabilitySpec(
            resource=str(spec["resource"]),
            rights=self._normalize_rights(spec.get("rights", [CapabilityRight.READ.value])),
            effect=CapabilityEffect(effect),
            constraints=dict(spec.get("constraints") or {}),
            metadata=dict(spec.get("metadata") or {}),
            expires_at=spec.get("expires_at"),
            uses_remaining=uses_remaining,
            delegable=bool(spec.get("delegable", False)),
            revocable=bool(spec.get("revocable", True)),
            max_delegation_depth=spec.get("max_delegation_depth"),
        )

    def _normalize_rights(self, rights: Iterable[str | CapabilityRight]) -> set[str]:
        try:
            normalized = {CapabilityRight(str(right)).value for right in rights}
        except ValueError as exc:
            raise ValidationError(f"unknown capability right: {exc}") from exc
        if not normalized:
            raise ValidationError("capability must include at least one right")
        if len(normalized) > self.config.capability.max_rights_per_capability:
            raise ValidationError("capability rights exceed configured limit")
        return normalized

    def _canonical_resource(self, resource: str) -> str:
        raw = str(resource).strip().replace("\\", "/")
        while "//" in raw:
            raw = raw.replace("//", "/")
        return raw.rstrip("/") if raw.endswith("/") and not raw.endswith(":/") else raw

    def _effect_from_legacy_constraints(self, constraints: dict[str, Any]) -> tuple[CapabilityEffect, int | None]:
        return self._effect_from_policy(str(constraints.get(self.POLICY_KEY, self.ALWAYS_ALLOW)))

    def _effect_from_policy(self, policy: str) -> tuple[CapabilityEffect, int | None]:
        if policy == self.ALWAYS_ALLOW:
            return CapabilityEffect.ALLOW, None
        if policy == self.ALWAYS_DENY:
            return CapabilityEffect.DENY, None
        if policy == self.ASK_EACH_TIME:
            return CapabilityEffect.ASK, None
        if policy == self.ALLOW_ONCE:
            return CapabilityEffect.ALLOW, 1
        raise ValueError(f"unknown permission policy: {policy}")

    def _validate_constraints(self, constraints: dict[str, Any]) -> None:
        try:
            size = len(json.dumps(constraints, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        except TypeError as exc:
            raise ValidationError("capability constraints must be JSON-serializable") from exc
        if size > self.config.capability.max_constraints_bytes:
            raise ValidationError("capability constraints exceed configured byte limit")

    def _evaluate_constraints(self, cap: Capability, context: dict[str, Any]) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for key, value in cap.constraints.items():
            if key not in self._KNOWN_CONSTRAINT_KEYS:
                results[key] = {"ok": False, "reason": "unknown constraint key"}
                continue
            results[key] = {"ok": True, "value": value}
        return results

    def _require_constraint_attenuation(self, parent_cap: Capability, spec: CapabilitySpec) -> None:
        for key, value in parent_cap.constraints.items():
            if spec.constraints.get(key) != value:
                raise CapabilityDenied(f"delegated capability cannot drop parent constraint: {key}")
        for key, value in spec.constraints.items():
            if key in parent_cap.constraints:
                continue
            if key not in self._KNOWN_CONSTRAINT_KEYS:
                raise CapabilityDenied(f"delegated capability uses unknown constraint: {key}")
            if key == self.config.shell.policy_capability_key:
                raise CapabilityDenied(f"delegated constraint is not covered by parent: {key}")

    def _context_dict(self, context: OperationContext | dict[str, Any] | None) -> dict[str, Any]:
        if context is None:
            return {}
        if isinstance(context, OperationContext):
            return {
                "primitive": context.primitive,
                "operation": context.operation,
                **context.metadata,
            }
        return dict(context)

    def _is_expired(self, cap: Capability) -> bool:
        return cap.expires_at is not None and cap.expires_at <= utc_now()

    def _is_trusted_issuer(self, actor: str) -> bool:
        if actor in self.config.capability.trusted_issuers:
            return True
        return any(actor.startswith(prefix) for prefix in self.config.capability.trusted_issuer_prefixes)

    def _issuer_chain(self, cap: Capability) -> list[str]:
        chain: list[str] = []
        current: Capability | None = cap
        seen: set[str] = set()
        while current is not None and current.cap_id not in seen:
            seen.add(current.cap_id)
            chain.append(current.cap_id)
            parent_id = current.parent_cap_id or current.issuer_cap_id
            current = self.store.get_capability(parent_id) if parent_id else None
        return chain

    def _record_decision(self, decision: CapabilityDecision, *, audit: bool) -> CapabilityDecision:
        if audit:
            self.audit.record(
                actor=decision.subject,
                action="capability.authorize",
                target=decision.resource,
                capability_refs=decision.matched_capability_ids,
                decision=self._decision_json(decision),
            )
        return decision

    def _decision_json(self, decision: CapabilityDecision) -> dict[str, Any]:
        return {
            "subject": decision.subject,
            "resource": decision.resource,
            "right": decision.right,
            "allowed": decision.allowed,
            "effect": decision.effect.value if decision.effect else None,
            "policy": decision.policy,
            "reason": decision.reason,
            "matched_capability_ids": decision.matched_capability_ids,
            "selected_capability_id": decision.selected_capability_id,
            "consume_capability_id": decision.consume_capability_id,
            "human_request_id": decision.human_request_id,
            "issuer_chain": decision.issuer_chain,
            "constraint_results": decision.constraint_results,
            "context": self._preview_context(decision.context),
        }

    def _preview_context(self, context: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
        limit = self.config.capability.decision_explain_preview_chars
        if len(text) <= limit:
            return context
        return {"preview": text[:limit], "truncated": True}

    def _capability_json(self, cap: Capability) -> dict[str, Any]:
        return {
            "cap_id": cap.cap_id,
            "subject": cap.subject,
            "resource": cap.resource,
            "rights": sorted(cap.rights),
            "effect": cap.effect.value,
            "issuer": cap.issued_by,
            "issuer_cap_id": cap.issuer_cap_id,
            "parent_cap_id": cap.parent_cap_id,
            "delegation_depth": cap.delegation_depth,
            "issued_at": cap.issued_at,
            "expires_at": cap.expires_at,
            "uses_remaining": cap.uses_remaining,
            "status": cap.status.value,
            "delegable": cap.delegable,
            "revocable": cap.revocable,
            "constraints": cap.constraints,
            "metadata": cap.metadata,
        }

    def _attach_to_process(self, subject: str, cap_id: str) -> None:
        process = self.store.get_process(subject)
        if process is None:
            return
        if cap_id not in process.capabilities:
            process.capabilities.append(cap_id)
            process.updated_at = utc_now()
            self.store.update_process(process)

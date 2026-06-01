from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models.exceptions import CapabilityDenied, NotFound
from agent_libos.utils.ids import new_id, utc_now
from agent_libos.models import Capability, CapabilityRight, EventType, ObjectHandle
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.storage import SQLiteStore


class CapabilityManager:
    """Capability directory and permission-policy helper."""

    POLICY_KEY = "permission_policy"
    ALWAYS_ALLOW = "always_allow"
    ALWAYS_DENY = "always_deny"
    ASK_EACH_TIME = "ask_each_time"
    ALLOW_ONCE = "allow_once"
    MISSING = "missing"
    POLICY_VALUES = {ALWAYS_ALLOW, ALWAYS_DENY, ASK_EACH_TIME, ALLOW_ONCE}

    def __init__(self, store: SQLiteStore, audit: AuditManager, events: EventBus, config: AgentLibOSConfig | None = None):
        self.config = config or DEFAULT_CONFIG
        self.store = store
        self.audit = audit
        self.events = events

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
        cap = Capability(
            cap_id=new_id("cap"),
            subject=subject,
            resource=resource,
            rights={str(right) for right in rights},
            constraints=constraints or {},
            issued_by=issued_by,
            issued_at=utc_now(),
            expires_at=expires_at,
            delegable=delegable,
            revocable=revocable,
            revoked=False,
        )
        self.store.insert_capability(cap)
        self._attach_to_process(subject, cap.cap_id)
        self.events.emit(
            EventType.CAPABILITY_GRANTED,
            source=issued_by,
            target=subject,
            payload={"capability_id": cap.cap_id, "resource": resource, "rights": sorted(cap.rights)},
        )
        self.audit.record(
            actor=issued_by,
            action="capability.grant",
            target=f"{subject}:{resource}",
            capability_refs=[cap.cap_id],
            decision={"granted": True, "rights": sorted(cap.rights)},
        )
        return cap

    def revoke(self, cap_id: str, revoked_by: str = "system", reason: str | None = None) -> Capability:
        cap = self.store.get_capability(cap_id)
        if cap is None:
            raise NotFound(f"capability not found: {cap_id}")
        if not cap.revocable:
            raise CapabilityDenied(f"capability is not revocable: {cap_id}")
        revoked = replace(cap, revoked=True)
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
            decision={"revoked": True, "reason": reason},
        )
        return revoked

    def check(self, subject: str, resource: str, right: str | CapabilityRight) -> bool:
        # ASK_EACH_TIME is intentionally not treated as allowed here; only the
        # primitive with enough operation context may turn it into a human prompt.
        return self.permission_policy(subject, resource, right) in {self.ALWAYS_ALLOW, self.ALLOW_ONCE}

    def require(self, subject: str, resource: str, right: str | CapabilityRight) -> None:
        requested_right = str(right)
        policy = self.permission_policy(subject, resource, right)
        if policy in {self.ALWAYS_ALLOW, self.ALLOW_ONCE}:
            return
        if policy == self.ALWAYS_DENY:
            raise CapabilityDenied(f"{subject} denied {requested_right} on {resource}")
        if policy == self.ASK_EACH_TIME:
            raise CapabilityDenied(f"{subject} requires human approval for {requested_right} on {resource}")
        raise CapabilityDenied(f"{subject} lacks {requested_right} on {resource}")

    def permission_policy(self, subject: str, resource: str, right: str | CapabilityRight) -> str:
        matches = self._matching_capabilities(subject, resource, right)
        if not matches:
            return self.MISSING
        cap = matches[0]
        return str(cap.constraints.get(self.POLICY_KEY) or self.ALWAYS_ALLOW)

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
        merged_constraints = dict(constraints or {})
        merged_constraints[self.POLICY_KEY] = policy
        cap = self.grant(
            subject=subject,
            resource=resource,
            rights=rights,
            issued_by=issued_by or self.config.runtime.default_human_actor,
            constraints=merged_constraints,
        )
        actor = issued_by or self.config.runtime.default_human_actor
        self.audit.record(
            actor=actor,
            action="capability.permission_policy",
            target=f"{subject}:{resource}",
            capability_refs=[cap.cap_id],
            decision={"policy": policy, "rights": sorted(cap.rights)},
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
        return self.set_permission_policy(
            subject=subject,
            resource=resource,
            rights=rights,
            policy=self.ALLOW_ONCE,
            issued_by=issued_by or self.config.runtime.default_human_actor,
            constraints=constraints,
        )

    def inherit(
        self,
        parent: str,
        child: str,
        resource: str,
        rights: Iterable[str | CapabilityRight],
        issued_by: str,
        constraints: dict | None = None,
    ) -> Capability:
        normalized = {str(right) for right in rights}
        if not normalized:
            raise ValueError("inherited capability must include at least one right")
        for right in normalized:
            policy = self.permission_policy(parent, resource, right)
            if policy != self.ALWAYS_ALLOW:
                raise CapabilityDenied(
                    f"{parent} cannot inherit {right} on {resource} to {child}; parent policy is {policy}"
                )
        inherited_constraints = dict(constraints or {})
        inherited_constraints[self.POLICY_KEY] = self.ALWAYS_ALLOW
        inherited_constraints["inherited_from"] = parent
        cap = self.grant(
            subject=child,
            resource=resource,
            rights=normalized,
            issued_by=issued_by,
            constraints=inherited_constraints,
        )
        self.audit.record(
            actor=issued_by,
            action="capability.inherit",
            target=f"{parent}->{child}:{resource}",
            capability_refs=[cap.cap_id],
            decision={"rights": sorted(normalized)},
        )
        return cap

    def consume_allow_once(self, subject: str, resource: str, right: str | CapabilityRight, used_by: str) -> None:
        # One-shot grants are consumed by the primitive after the operation
        # succeeds, so a failed attempt does not burn the user's approval.
        for cap in self._matching_capabilities(subject, resource, right):
            if cap.constraints.get(self.POLICY_KEY) == self.ALLOW_ONCE:
                self.revoke(cap.cap_id, revoked_by=used_by, reason="one-time permission consumed")
                return

    def assert_handle(self, subject: str, handle: ObjectHandle, right: str | CapabilityRight) -> None:
        requested = str(right)
        if requested not in handle.rights and "*" not in handle.rights:
            raise CapabilityDenied(f"object handle lacks {requested}: {handle.oid}")
        cap = self.store.get_capability(handle.capability_id)
        if cap is None or cap.revoked:
            raise CapabilityDenied(f"invalid object capability: {handle.capability_id}")
        if cap.subject != subject:
            raise CapabilityDenied(f"capability subject mismatch: {cap.subject} != {subject}")
        if not self._resource_matches(cap.resource, f"object:{handle.oid}"):
            raise CapabilityDenied(f"capability does not target object: {handle.oid}")
        if requested not in cap.rights and "*" not in cap.rights:
            raise CapabilityDenied(f"capability lacks {requested}: {handle.oid}")

    def handle_for_object(
        self,
        subject: str,
        oid: str,
        rights: Iterable[str | CapabilityRight],
        issued_by: str = "system",
        expires_at: str | None = None,
    ) -> ObjectHandle:
        normalized = {str(right) for right in rights}
        cap = self.grant(
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

    def spec(
        self,
        resource: str,
        rights: Iterable[str | CapabilityRight],
        **kwargs,
    ) -> dict:
        return {"resource": resource, "rights": [str(right) for right in rights], **kwargs}

    def tool_execute(self, tool: str, rights: Iterable[str | CapabilityRight] | None = None, **kwargs) -> dict:
        resource = tool if tool.startswith("tool:") else f"tool:{tool}"
        return self.spec(resource, rights or [CapabilityRight.EXECUTE], **kwargs)

    def project_read(self, name: str, **kwargs) -> dict:
        return self.spec(f"project:{name}", [CapabilityRight.READ], **kwargs)

    def object_access(self, oid: str, rights: Iterable[str | CapabilityRight], **kwargs) -> dict:
        return self.spec(f"object:{oid}", rights, **kwargs)

    def _attach_to_process(self, subject: str, cap_id: str) -> None:
        process = self.store.get_process(subject)
        if process is None:
            return
        if cap_id not in process.capabilities:
            process.capabilities.append(cap_id)
            process.updated_at = utc_now()
            self.store.update_process(process)

    def _resource_matches(self, granted: str, requested: str) -> bool:
        if granted == "*" or granted == requested:
            return True
        if granted.endswith(":*"):
            return requested.startswith(granted[:-1])
        if granted.endswith("/*"):
            return requested.startswith(granted[:-1])
        return False

    def _matching_capabilities(self, subject: str, resource: str, right: str | CapabilityRight) -> list[Capability]:
        requested_right = str(right)
        now = utc_now()
        matches: list[Capability] = []
        for cap in self.store.list_capabilities(subject=subject):
            if cap.revoked:
                continue
            if cap.expires_at is not None and cap.expires_at <= now:
                continue
            if not self._resource_matches(cap.resource, resource):
                continue
            if "*" not in cap.rights and requested_right not in cap.rights:
                continue
            matches.append(cap)
        # Prefer the most specific resource grant when overlapping wildcard and
        # path-level capabilities both exist.
        matches.sort(key=lambda cap: (len(cap.resource), cap.issued_at), reverse=True)
        return matches

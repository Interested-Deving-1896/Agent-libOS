from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from agent_libos.exceptions import CapabilityDenied, NotFound
from agent_libos.ids import new_id, utc_now
from agent_libos.models import Capability, CapabilityRight, EventType, ObjectHandle
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.storage import SQLiteStore


class CapabilityManager:
    def __init__(self, store: SQLiteStore, audit: AuditManager, events: EventBus):
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
        requested_right = str(right)
        now = utc_now()
        for cap in self.store.list_capabilities(subject=subject):
            if cap.revoked:
                continue
            if cap.expires_at is not None and cap.expires_at <= now:
                continue
            if not self._resource_matches(cap.resource, resource):
                continue
            if "*" in cap.rights or requested_right in cap.rights:
                return True
        return False

    def require(self, subject: str, resource: str, right: str | CapabilityRight) -> None:
        if not self.check(subject, resource, right):
            raise CapabilityDenied(f"{subject} lacks {right} on {resource}")

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

    def tool_call(self, tool: str, rights: Iterable[str | CapabilityRight] | None = None, **kwargs) -> dict:
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
        return False

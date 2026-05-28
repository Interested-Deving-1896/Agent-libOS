from __future__ import annotations

import builtins
from collections.abc import Callable
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.models import CapabilityRight
from agent_libos.exceptions import NotFound
from agent_libos.ids import new_id, utc_now
from agent_libos.models import (
    EventType,
    HumanRequest,
    HumanRequestStatus,
    ProcessSignal,
    ProcessStatus,
)
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.storage import SQLiteStore


class HumanObjectManager:
    def __init__(
        self,
        store: SQLiteStore,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        output_sink: Callable[[str], None] | None = None,
    ):
        self.store = store
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        self.output_sink = output_sink or (lambda message: print(message, flush=True))

    def query(
        self,
        pid: str,
        human: str,
        request: dict[str, Any],
        blocking: bool = True,
    ) -> str:
        now = utc_now()
        human_request = HumanRequest(
            request_id=new_id("hreq"),
            pid=pid,
            human=human,
            payload=request,
            status=HumanRequestStatus.PENDING,
            decision=None,
            blocking=blocking,
            created_at=now,
            updated_at=now,
        )
        self.store.insert_human_request(human_request)
        if blocking:
            process = self.store.get_process(pid)
            if process is not None:
                process.status = ProcessStatus.WAITING_HUMAN
                process.status_message = f"waiting for human request {human_request.request_id}"
                process.updated_at = utc_now()
                self.store.update_process(process)
        self.events.emit(
            EventType.HUMAN_QUERY,
            source=pid,
            target=f"human:{human}",
            payload={"request_id": human_request.request_id, "request": request, "blocking": blocking},
        )
        self.audit.record(
            actor=pid,
            action="human.query",
            target=f"human:{human}",
            decision={"request_id": human_request.request_id, "blocking": blocking, "request": request},
        )
        return human_request.request_id

    def approve(
        self,
        request_id: str,
        decision: dict[str, Any] | None = None,
        responder: str = "human:owner",
    ) -> HumanRequest:
        return self._decide(request_id, HumanRequestStatus.APPROVED, decision or {"approved": True}, responder)

    def reject(
        self,
        request_id: str,
        decision: dict[str, Any] | None = None,
        responder: str = "human:owner",
    ) -> HumanRequest:
        return self._decide(request_id, HumanRequestStatus.REJECTED, decision or {"approved": False}, responder)

    def interrupt(self, pid: str, signal: ProcessSignal | str, payload: dict[str, Any] | None = None) -> str:
        sig = ProcessSignal(signal)
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        if sig == ProcessSignal.PAUSE:
            process.status = ProcessStatus.PAUSED
        elif sig == ProcessSignal.RESUME:
            process.status = ProcessStatus.RUNNABLE
        elif sig in {ProcessSignal.CANCEL, ProcessSignal.TERMINATE}:
            process.status = ProcessStatus.KILLED
        process.status_message = (payload or {}).get("reason")
        process.updated_at = utc_now()
        self.store.update_process(process)
        event = self.events.emit(
            EventType.PROCESS_SIGNAL,
            source="human",
            target=pid,
            payload={"signal": sig.value, "payload": payload or {}},
        )
        self.audit.record(
            actor="human",
            action="human.interrupt",
            target=f"process:{pid}",
            decision={"signal": sig.value, "payload": payload or {}},
        )
        return event.event_id

    def output(
        self,
        pid: str,
        message: str,
        human: str = "owner",
        channel: str = "terminal",
    ) -> dict[str, Any]:
        selected_channel = "terminal" if channel != "terminal" else channel
        resource = f"human:{human}"
        self.capabilities.require(pid, resource, CapabilityRight.WRITE)
        self.output_sink(message)
        self.events.emit(
            EventType.HUMAN_OUTPUT,
            source=pid,
            target=resource,
            payload={"channel": selected_channel, "chars": len(message)},
        )
        self.audit.record(
            actor=pid,
            action="human.output",
            target=resource,
            decision={"channel": selected_channel, "chars": len(message)},
        )
        return {"delivered": True, "channel": selected_channel, "chars": len(message)}

    def get(self, request_id: str) -> HumanRequest:
        request = self.store.get_human_request(request_id)
        if request is None:
            raise NotFound(f"human request not found: {request_id}")
        return request

    def list(self, pid: str | None = None) -> builtins.list[HumanRequest]:
        return self.store.list_human_requests(pid=pid)

    def _decide(
        self,
        request_id: str,
        status: HumanRequestStatus,
        decision: dict[str, Any],
        responder: str,
    ) -> HumanRequest:
        request = self.store.get_human_request(request_id)
        if request is None:
            raise NotFound(f"human request not found: {request_id}")
        request.status = status
        request.decision = decision
        request.updated_at = utc_now()
        self.store.update_human_request(request)
        if status == HumanRequestStatus.APPROVED:
            cap_spec = request.payload.get("requested_capability")
            if isinstance(cap_spec, dict):
                resource = cap_spec.get("resource")
                if not isinstance(resource, str):
                    raise ValueError("requested capability must include a string resource")
                subject = cap_spec.get("subject", request.pid)
                if not isinstance(subject, str):
                    subject = request.pid
                rights = cap_spec.get("rights", ["execute"])
                if not isinstance(rights, list):
                    rights = ["execute"]
                constraints = cap_spec.get("constraints")
                expires_at = cap_spec.get("expires_at")
                self.capabilities.grant(
                    subject=subject,
                    resource=resource,
                    rights=rights,
                    issued_by=responder,
                    constraints=constraints if isinstance(constraints, dict) else None,
                    expires_at=expires_at if isinstance(expires_at, str) else None,
                    delegable=bool(cap_spec.get("delegable", False)),
                )
        process = self.store.get_process(request.pid)
        if process is not None and process.status == ProcessStatus.WAITING_HUMAN:
            process.status = ProcessStatus.RUNNABLE if status == HumanRequestStatus.APPROVED else ProcessStatus.PAUSED
            process.status_message = None if status == HumanRequestStatus.APPROVED else f"human rejected {request_id}"
            process.updated_at = utc_now()
            self.store.update_process(process)
        self.events.emit(
            EventType.HUMAN_RESPONSE,
            source=responder,
            target=request.pid,
            payload={"request_id": request_id, "status": status.value, "decision": decision},
        )
        self.audit.record(
            actor=responder,
            action="human.response",
            target=f"human_request:{request_id}",
            decision={"status": status.value, "decision": decision},
        )
        return request

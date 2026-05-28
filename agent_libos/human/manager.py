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

    def request_permission(
        self,
        pid: str,
        human: str,
        resource: str,
        rights: list[str],
        reason: str,
        blocking: bool = True,
    ) -> str:
        return self.query(
            pid=pid,
            human=human,
            request={
                "type": "permission_request",
                "question": f"Set permission policy for {resource} rights={rights}: {reason}",
                "requested_permission": {
                    "subject": pid,
                    "resource": resource,
                    "rights": rights,
                },
                "context": {"reason": reason},
            },
            blocking=blocking,
        )

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
        request = HumanRequest(
            request_id=new_id("hreq"),
            pid=pid,
            human=human,
            payload={"type": "output", "message": message, "channel": selected_channel},
            status=HumanRequestStatus.PENDING,
            decision=None,
            blocking=False,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.store.insert_human_request(request)
        delivered = self._deliver_output_request(request)
        return {
            "delivered": True,
            "request_id": delivered.request_id,
            "channel": selected_channel,
            "chars": len(message),
        }

    def get(self, request_id: str) -> HumanRequest:
        request = self.store.get_human_request(request_id)
        if request is None:
            raise NotFound(f"human request not found: {request_id}")
        return request

    def list(self, pid: str | None = None) -> builtins.list[HumanRequest]:
        return self.store.list_human_requests(pid=pid)

    def pending(self, human: str | None = None) -> builtins.list[HumanRequest]:
        requests = [request for request in self.store.list_human_requests() if request.status == HumanRequestStatus.PENDING]
        if human is not None:
            requests = [request for request in requests if request.human == human]
        return requests

    def process_next_terminal(
        self,
        human: str = "owner",
        auto_approve: bool | None = None,
        auto_policy: str | None = None,
        input_fn: Callable[[str], str] | None = None,
    ) -> HumanRequest | None:
        pending = self.pending(human=human)
        if not pending:
            return None
        request = pending[0]
        request_type = request.payload.get("type")
        if request_type == "output":
            return self._deliver_output_request(request)
        question = self._terminal_question(request)
        if request_type == "permission_request":
            policy = self._select_permission_policy(
                question=question,
                auto_policy=auto_policy,
                auto_approve=auto_approve,
                input_fn=input_fn,
            )
            decision = {"policy": policy, "source": "terminal_queue"}
            if policy == CapabilityManager.ALWAYS_DENY:
                return self.reject(request.request_id, {"approved": False, **decision})
            return self.approve(request.request_id, {"approved": True, **decision})

        approved = self._select_boolean_approval(
            question=question,
            auto_approve=auto_approve,
            input_fn=input_fn,
        )
        if approved:
            return self.approve(request.request_id, {"approved": True, "source": "terminal_queue"})
        return self.reject(request.request_id, {"approved": False, "source": "terminal_queue"})

    def drain_terminal_queue(
        self,
        human: str = "owner",
        auto_approve: bool | None = None,
        auto_policy: str | None = None,
        input_fn: Callable[[str], str] | None = None,
    ) -> builtins.list[HumanRequest]:
        processed: builtins.list[HumanRequest] = []
        while True:
            request = self.process_next_terminal(
                human=human,
                auto_approve=auto_approve,
                auto_policy=auto_policy,
                input_fn=input_fn,
            )
            if request is None:
                return processed
            processed.append(request)

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
        permission_related = False
        permission_spec = request.payload.get("requested_permission")
        if isinstance(permission_spec, dict):
            permission_related = True
            resource = permission_spec.get("resource")
            if not isinstance(resource, str):
                raise ValueError("requested permission must include a string resource")
            subject = permission_spec.get("subject", request.pid)
            if not isinstance(subject, str):
                subject = request.pid
            rights = permission_spec.get("rights", ["execute"])
            if not isinstance(rights, list):
                rights = ["execute"]
            policy = str(
                decision.get(
                    "policy",
                    CapabilityManager.ALWAYS_ALLOW if status == HumanRequestStatus.APPROVED else CapabilityManager.ALWAYS_DENY,
                )
            )
            if policy not in {
                CapabilityManager.ALWAYS_ALLOW,
                CapabilityManager.ALWAYS_DENY,
                CapabilityManager.ASK_EACH_TIME,
            }:
                raise ValueError(f"unknown permission policy: {policy}")
            constraints = permission_spec.get("constraints")
            self.capabilities.set_permission_policy(
                subject=subject,
                resource=resource,
                rights=rights,
                policy=policy,
                issued_by=responder,
                constraints=constraints if isinstance(constraints, dict) else None,
            )

        once_spec = request.payload.get("requested_once_capability")
        if isinstance(once_spec, dict):
            permission_related = True
            if status == HumanRequestStatus.APPROVED:
                resource = once_spec.get("resource")
                if not isinstance(resource, str):
                    raise ValueError("requested one-time capability must include a string resource")
                subject = once_spec.get("subject", request.pid)
                if not isinstance(subject, str):
                    subject = request.pid
                rights = once_spec.get("rights", ["execute"])
                if not isinstance(rights, list):
                    rights = ["execute"]
                constraints = once_spec.get("constraints")
                self.capabilities.grant_once(
                    subject=subject,
                    resource=resource,
                    rights=rights,
                    issued_by=responder,
                    constraints=constraints if isinstance(constraints, dict) else None,
                )

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
            process.status = (
                ProcessStatus.RUNNABLE
                if status == HumanRequestStatus.APPROVED or permission_related
                else ProcessStatus.PAUSED
            )
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

    def _select_permission_policy(
        self,
        question: str,
        auto_policy: str | None,
        auto_approve: bool | None,
        input_fn: Callable[[str], str] | None,
    ) -> str:
        choices = {
            CapabilityManager.ALWAYS_ALLOW,
            CapabilityManager.ALWAYS_DENY,
            CapabilityManager.ASK_EACH_TIME,
        }
        if auto_policy is not None:
            if auto_policy not in choices:
                raise ValueError(f"unknown permission policy: {auto_policy}")
            self.output_sink(f"{question} [policy={auto_policy}]")
            return auto_policy
        if auto_approve is not None:
            policy = CapabilityManager.ALWAYS_ALLOW if auto_approve else CapabilityManager.ALWAYS_DENY
            self.output_sink(f"{question} [policy={policy}]")
            return policy
        reader = input_fn or input
        answer = reader(
            f"{question} [a=always allow, d=always deny, e=ask each time; default=d]: "
        ).strip().lower()
        return {
            "a": CapabilityManager.ALWAYS_ALLOW,
            "allow": CapabilityManager.ALWAYS_ALLOW,
            "always_allow": CapabilityManager.ALWAYS_ALLOW,
            "y": CapabilityManager.ALWAYS_ALLOW,
            "yes": CapabilityManager.ALWAYS_ALLOW,
            "d": CapabilityManager.ALWAYS_DENY,
            "deny": CapabilityManager.ALWAYS_DENY,
            "always_deny": CapabilityManager.ALWAYS_DENY,
            "n": CapabilityManager.ALWAYS_DENY,
            "no": CapabilityManager.ALWAYS_DENY,
            "e": CapabilityManager.ASK_EACH_TIME,
            "each": CapabilityManager.ASK_EACH_TIME,
            "ask": CapabilityManager.ASK_EACH_TIME,
            "ask_each_time": CapabilityManager.ASK_EACH_TIME,
        }.get(answer, CapabilityManager.ALWAYS_DENY)

    def _terminal_question(self, request: HumanRequest) -> str:
        question = str(request.payload.get("question") or request.payload)
        if request.payload.get("type") != "external_operation_approval":
            return question
        context = request.payload.get("context")
        if not isinstance(context, dict):
            return question
        capability = request.payload.get("requested_once_capability")
        lines = [question, "", "Operation details:"]
        for label, key in [
            ("process", "pid"),
            ("primitive", "primitive"),
            ("operation", "operation"),
            ("path", "path"),
            ("absolute path", "absolute_path"),
            ("resource", "resource"),
            ("grant scope", "grant_scope"),
            ("encoding", "encoding"),
            ("overwrite flag", "overwrite"),
            ("will create", "will_create"),
            ("will overwrite", "will_overwrite"),
            ("content bytes", "content_bytes"),
            ("content sha256", "content_sha256"),
        ]:
            if key in context:
                lines.append(f"- {label}: {context[key]}")
        target = context.get("target")
        if isinstance(target, dict):
            lines.append("- target:")
            for key in ["exists", "kind", "size_bytes", "modified_at"]:
                if key in target:
                    lines.append(f"  - {key}: {target[key]}")
        if isinstance(capability, dict):
            lines.append("- one-time capability:")
            lines.append(f"  - resource: {capability.get('resource')}")
            lines.append(f"  - rights: {capability.get('rights')}")
        preview = context.get("content_preview")
        if isinstance(preview, str):
            truncated = bool(context.get("content_preview_truncated"))
            lines.append(f"- content preview{' (truncated)' if truncated else ''}:")
            lines.append(self._indent_block(preview))
        return "\n".join(lines) + "\n"

    def _indent_block(self, text: str) -> str:
        if not text:
            return "  <empty>"
        return "\n".join(f"  {line}" for line in text.splitlines() or [text])

    def _select_boolean_approval(
        self,
        question: str,
        auto_approve: bool | None,
        input_fn: Callable[[str], str] | None,
    ) -> bool:
        if auto_approve is None:
            reader = input_fn or input
            answer = reader(f"{question} [y/N]: ").strip().lower()
            return answer in {"y", "yes"}
        self.output_sink(f"{question} [{'approved' if auto_approve else 'rejected'}]")
        return auto_approve

    def _deliver_output_request(self, request: HumanRequest) -> HumanRequest:
        message = str(request.payload.get("message", ""))
        channel = str(request.payload.get("channel", "terminal"))
        self.output_sink(message)
        request.status = HumanRequestStatus.DELIVERED
        request.decision = {"delivered": True}
        request.updated_at = utc_now()
        self.store.update_human_request(request)
        resource = f"human:{request.human}"
        self.events.emit(
            EventType.HUMAN_OUTPUT,
            source=request.pid,
            target=resource,
            payload={"request_id": request.request_id, "channel": channel, "chars": len(message)},
        )
        self.audit.record(
            actor=request.pid,
            action="human.output",
            target=resource,
            decision={"request_id": request.request_id, "channel": channel, "chars": len(message), "queued": True},
        )
        return request

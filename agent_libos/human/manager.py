from __future__ import annotations

import asyncio
import builtins
from typing import TYPE_CHECKING, Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models import CapabilityRight, ProcessMessage, ProcessMessageKind
from agent_libos.models.exceptions import CapabilityDenied, HumanResponseRequired, NotFound, ValidationError
from agent_libos.utils.ids import new_id, utc_now
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
from agent_libos.substrate import HumanProvider

if TYPE_CHECKING:
    from agent_libos.runtime.message_manager import ProcessMessageManager


class HumanObjectManager:
    """HumanObject primitive: terminal queue, approvals, questions, and output."""

    def __init__(
        self,
        store: SQLiteStore,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        provider: HumanProvider,
        config: AgentLibOSConfig | None = None,
    ):
        self.config = config or DEFAULT_CONFIG
        self.store = store
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        self.provider = provider
        self._messages: ProcessMessageManager | None = None

    def bind_messages(self, messages: "ProcessMessageManager") -> None:
        self._messages = messages

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
            # Blocking human requests suspend scheduling for this process until
            # a terminal queue decision moves it back to RUNNABLE.
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

    def ask(
        self,
        pid: str,
        question: str,
        human: str | None = None,
        context: dict[str, Any] | None = None,
        blocking: bool = True,
    ) -> str:
        selected_human = human or self.config.runtime.default_human
        resource = f"human:{selected_human}"
        self.capabilities.require(pid, resource, CapabilityRight.WRITE)
        return self.query(
            pid=pid,
            human=selected_human,
            request={
                "type": "question",
                "question": question,
                "context": context or {},
            },
            blocking=blocking,
        )

    def answer_for_request(self, request_id: str) -> str:
        request = self.get(request_id)
        if request.payload.get("type") != "question":
            raise ValidationError(f"human request is not a question: {request_id}")
        if request.status == HumanRequestStatus.PENDING:
            raise HumanResponseRequired(
                request_id=request_id,
                message=f"{request.pid} is waiting for human answer to {request_id}",
            )
        if request.status != HumanRequestStatus.APPROVED:
            raise CapabilityDenied(f"human question {request_id} was not answered: {request.status.value}")
        decision = request.decision or {}
        if "answer" not in decision:
            raise ValidationError(f"human question {request_id} has no answer")
        return str(decision["answer"])

    def approve(
        self,
        request_id: str,
        decision: dict[str, Any] | None = None,
        responder: str | None = None,
    ) -> HumanRequest:
        return self._decide(
            request_id,
            HumanRequestStatus.APPROVED,
            decision or {"approved": True},
            responder or self.config.runtime.default_human_actor,
        )

    def reject(
        self,
        request_id: str,
        decision: dict[str, Any] | None = None,
        responder: str | None = None,
    ) -> HumanRequest:
        return self._decide(
            request_id,
            HumanRequestStatus.REJECTED,
            decision or {"approved": False},
            responder or self.config.runtime.default_human_actor,
        )

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

    def send_process_message(
        self,
        recipient_pid: str,
        body: str,
        *,
        kind: ProcessMessageKind | str = ProcessMessageKind.NORMAL,
        human: str | None = None,
        channel: str = "human",
        correlation_id: str | None = None,
        reply_to: str | None = None,
        subject: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ProcessMessage:
        if self._messages is None:
            raise RuntimeError("HumanObjectManager is not bound to a ProcessMessageManager")
        selected_human = human or self.config.runtime.default_human
        selected_kind = ProcessMessageKind(kind)
        message_payload = dict(payload or {})
        message_payload.setdefault("source", "human_input")
        message_payload.setdefault("human", selected_human)
        message = self._messages.post(
            sender=f"human:{selected_human}",
            recipient_pid=recipient_pid,
            kind=selected_kind,
            channel=channel,
            correlation_id=correlation_id,
            reply_to=reply_to,
            subject=subject if subject is not None else self._default_message_subject(selected_kind),
            body=body,
            payload=message_payload,
        )
        self.audit.record(
            actor=f"human:{selected_human}",
            action="human.message",
            target=f"process:{recipient_pid}",
            decision={
                "message_id": message.message_id,
                "kind": message.kind.value,
                "channel": message.channel,
                "correlation_id": message.correlation_id,
                "reply_to": message.reply_to,
                "subject": message.subject,
            },
        )
        return message

    def output(
        self,
        pid: str,
        message: str,
        human: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        selected_human = human or self.config.runtime.default_human
        selected_channel = channel or self.config.runtime.terminal_channel
        if selected_channel != self.config.runtime.terminal_channel:
            selected_channel = self.config.runtime.terminal_channel
        resource = f"human:{selected_human}"
        self.capabilities.require(pid, resource, CapabilityRight.WRITE)
        request = HumanRequest(
            request_id=new_id("hreq"),
            pid=pid,
            human=selected_human,
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
        human: str | None = None,
        auto_approve: bool | None = None,
        auto_policy: str | None = None,
        auto_answer: str | None = None,
    ) -> HumanRequest | None:
        selected_human = human or self.config.runtime.default_human
        pending = self.pending(human=selected_human)
        if not pending:
            return None
        # The terminal is the human's message queue. Process requests strictly
        # in creation order so approvals and answers remain predictable.
        request = pending[0]
        request_type = request.payload.get("type")
        if request_type == "output":
            return self._deliver_output_request(request)
        question = self._terminal_question(request)
        if request_type == "question":
            answer = self._select_text_answer(
                question=question,
                auto_answer=auto_answer,
            )
            return self.approve(
                request.request_id,
                {"approved": True, "answer": answer, "source": "terminal_queue"},
            )
        if request_type == "permission_request":
            policy = self._select_permission_policy(
                question=question,
                auto_policy=auto_policy,
                auto_approve=auto_approve,
            )
            decision = {"policy": policy, "source": "terminal_queue"}
            if policy == CapabilityManager.ALWAYS_DENY:
                return self.reject(request.request_id, {"approved": False, **decision})
            return self.approve(request.request_id, {"approved": True, **decision})

        approved = self._select_boolean_approval(
            question=question,
            auto_approve=auto_approve,
        )
        if approved:
            return self.approve(request.request_id, {"approved": True, "source": "terminal_queue"})
        return self.reject(request.request_id, {"approved": False, "source": "terminal_queue"})

    async def aprocess_next_terminal(
        self,
        human: str | None = None,
        auto_approve: bool | None = None,
        auto_policy: str | None = None,
        auto_answer: str | None = None,
    ) -> HumanRequest | None:
        return await asyncio.to_thread(
            self.process_next_terminal,
            human=human,
            auto_approve=auto_approve,
            auto_policy=auto_policy,
            auto_answer=auto_answer,
        )

    def drain_terminal_queue(
        self,
        human: str | None = None,
        auto_approve: bool | None = None,
        auto_policy: str | None = None,
        auto_answer: str | None = None,
    ) -> builtins.list[HumanRequest]:
        processed: builtins.list[HumanRequest] = []
        while True:
            request = self.process_next_terminal(
                human=human,
                auto_approve=auto_approve,
                auto_policy=auto_policy,
                auto_answer=auto_answer,
            )
            if request is None:
                return processed
            processed.append(request)

    async def adrain_terminal_queue(
        self,
        human: str | None = None,
        auto_approve: bool | None = None,
        auto_policy: str | None = None,
        auto_answer: str | None = None,
    ) -> builtins.list[HumanRequest]:
        processed: builtins.list[HumanRequest] = []
        while True:
            request = await self.aprocess_next_terminal(
                human=human,
                auto_approve=auto_approve,
                auto_policy=auto_policy,
                auto_answer=auto_answer,
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
            # Permission denials still wake the process so it can observe the
            # failed operation and explain what happened. Generic rejected human
            # approvals remain a pause/interruption signal.
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
    ) -> str:
        choices = {
            CapabilityManager.ALWAYS_ALLOW,
            CapabilityManager.ALWAYS_DENY,
            CapabilityManager.ASK_EACH_TIME,
        }
        if auto_policy is not None:
            if auto_policy not in choices:
                raise ValueError(f"unknown permission policy: {auto_policy}")
            self.provider.write(f"{question} [policy={auto_policy}]")
            return auto_policy
        if auto_approve is not None:
            policy = CapabilityManager.ALWAYS_ALLOW if auto_approve else CapabilityManager.ALWAYS_DENY
            self.provider.write(f"{question} [policy={policy}]")
            return policy
        answer = self.provider.read(
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
        if request.payload.get("type") == "question":
            context = request.payload.get("context")
            if not isinstance(context, dict) or not context:
                return question
            lines = [question, "Context:"]
            for key in sorted(context):
                lines.append(f"- {key}: {context[key]!r}")
            return "\n".join(lines)
        if request.payload.get("type") != "external_operation_approval":
            return question
        context = request.payload.get("context")
        if not isinstance(context, dict):
            return question
        # External-operation prompts show structured facts, not tool prose, so
        # the human can judge the primitive-level side effect safely.
        capability = request.payload.get("requested_once_capability")
        lines = ["Operation details:"]
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
            ("parents flag", "parents"),
            ("exist ok", "exist_ok"),
            ("recursive", "recursive"),
            ("missing ok", "missing_ok"),
            ("will create", "will_create"),
            ("will overwrite", "will_overwrite"),
            ("content bytes", "content_bytes"),
            ("content sha256", "content_sha256"),
            ("working directory", "working_directory"),
            ("argv", "argv"),
            ("command", "command"),
            ("timeout seconds", "timeout_s"),
            ("policy level", "policy_level"),
            ("policy reason", "policy_reason"),
            ("matched rule", "matched_rule"),
            ("high risk", "high_risk"),
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
        lines.append(question)
        return "\n".join(lines)

    def _indent_block(self, text: str) -> str:
        if not text:
            return "  <empty>"
        return "\n".join(f"  {line}" for line in text.splitlines() or [text])

    def _select_boolean_approval(
        self,
        question: str,
        auto_approve: bool | None,
    ) -> bool:
        if auto_approve is None:
            answer = self.provider.read(f"{question} [y/N]: ").strip().lower()
            return answer in {"y", "yes"}
        self.provider.write(f"{question} [{'approved' if auto_approve else 'rejected'}]")
        return auto_approve

    def _select_text_answer(
        self,
        question: str,
        auto_answer: str | None,
    ) -> str:
        if auto_answer is not None:
            self.provider.write(f"{question} [answer={auto_answer!r}]")
            return auto_answer
        return self.provider.read(f"{question} ")

    def _deliver_output_request(self, request: HumanRequest) -> HumanRequest:
        message = str(request.payload.get("message", ""))
        channel = str(request.payload.get("channel", self.config.runtime.terminal_channel))
        self.provider.write(message)
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

    def _default_message_subject(self, kind: ProcessMessageKind) -> str:
        if kind == ProcessMessageKind.INTERRUPT:
            return "Human interrupt"
        return "Human message"

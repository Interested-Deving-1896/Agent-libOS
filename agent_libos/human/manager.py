from __future__ import annotations

import asyncio
import builtins
import hashlib
import threading
from typing import TYPE_CHECKING, Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.capability.rules import AUTHORITY_RULES_KEY
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models import (
    AuthorityRisk,
    CapabilityEffect,
    CapabilityRight,
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    ProcessMessage,
    ProcessMessageKind,
)
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
from agent_libos.runtime.external_effects import (
    abandon_external_effect_intent,
    begin_external_effect_intent,
    classify_external_effect,
    record_external_effect,
    require_external_effect_classifier,
)
from agent_libos.runtime.effect_binding import canonical_effect_hash
from agent_libos.storage import RuntimeStore
from agent_libos.substrate import HumanProvider, ProviderEffectNotStarted
from agent_libos.utils.serde import dumps, to_jsonable

if TYPE_CHECKING:
    from agent_libos.runtime.message_manager import ProcessMessageManager

_SENSITIVE_HUMAN_AUDIT_KEYS = frozenset({"answer", "context", "decision", "message", "payload", "question", "reason"})


def _json_size_bytes(value: Any) -> int:
    return len(dumps(to_jsonable(value)).encode("utf-8"))


def _ensure_json_size(value: Any, limit_bytes: int, label: str) -> int:
    size = _json_size_bytes(value)
    if size > limit_bytes:
        raise ValidationError(f"{label} exceeds {limit_bytes} bytes (got {size})")
    return size


def _sanitize_human_observability(value: Any, *, preview_chars: int = 256) -> dict[str, Any]:
    jsonable = to_jsonable(value)
    redacted = _redact_human_value(jsonable)
    encoded = dumps(jsonable).encode("utf-8")
    preview = dumps(redacted)
    return {
        "preview": preview[: max(0, preview_chars)],
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
        "truncated": len(preview) > max(0, preview_chars),
        "redacted": redacted != jsonable,
    }


def _redact_human_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if str(key).lower() in _SENSITIVE_HUMAN_AUDIT_KEYS else _redact_human_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_human_value(item) for item in value]
    return value


class HumanObjectManager:
    """HumanObject primitive: terminal queue, approvals, questions, and output."""

    TERMINAL_PROCESS_STATUSES = {ProcessStatus.EXITED, ProcessStatus.FAILED, ProcessStatus.KILLED}

    def __init__(
        self,
        store: RuntimeStore,
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
        # A terminal is a single human decision stream. Serialize queue-head
        # claims and durable transitions so concurrent drains cannot act on the
        # same request, but never hold this lock across blocking provider I/O.
        # It is re-entrant because approve()/reject() commit under the same
        # transition lock.
        self._terminal_lock = threading.RLock()
        self._terminal_claims: set[str] = set()

    def bind_messages(self, messages: "ProcessMessageManager") -> None:
        self._messages = messages

    def query(
        self,
        pid: str,
        human: str,
        request: dict[str, Any],
        blocking: bool = True,
    ) -> str:
        request = self._bind_external_operation_approval(request)
        _ensure_json_size(request, self.config.tools.human_request_payload_max_bytes, "human request payload")
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
        # Request persistence, scheduler suspension, and observability are one
        # commit. A caller may therefore safely restore a reserved one-shot
        # capability if this method raises: no pending request was left behind.
        with self.store.transaction():
            process = self.store.get_process(pid)
            if process is not None and process.status in self.TERMINAL_PROCESS_STATUSES:
                raise ValidationError(
                    f"terminal process cannot create human requests: {pid} status={process.status.value}"
                )
            self.store.insert_human_request(human_request)
            operations = getattr(self.store, "operation_manager", None)
            if operations is not None:
                operations.expect("approval")
                operations.link_evidence(
                    "human_request",
                    human_request.request_id,
                    "approval",
                    metadata={"status": human_request.status.value, "blocking": blocking},
                )
            if blocking:
                # Blocking human requests suspend scheduling for this process until
                # a terminal queue decision moves it back to RUNNABLE.
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
                decision={
                    "request_id": human_request.request_id,
                    "blocking": blocking,
                    "request": _sanitize_human_observability(request),
                },
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
        selected_human = human or self.config.runtime.default_human
        manifests = getattr(self.store, "authority_manifest_manager", None)
        if manifests is not None:
            manifests.assert_capability_request(pid, resource, rights)
        decision = self.capabilities.require(
            pid,
            f"human:{selected_human}",
            CapabilityRight.WRITE,
            consume=False,
        )
        request = self._permission_request_payload(pid, resource, rights, reason)
        reservation_id = self._reserve_one_time_decision(decision, used_by="human")
        try:
            request_id = self.query(
                pid=pid,
                human=selected_human,
                request=request,
                blocking=blocking,
            )
        except Exception:
            self._restore_one_time_decision(reservation_id)
            raise
        self._commit_one_time_decision(reservation_id)
        return request_id

    def _bind_external_operation_approval(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("type") != "external_operation_approval":
            return request
        context = request.get("context")
        if not isinstance(context, dict):
            raise ValidationError("external operation approval requires an object context")
        selected = dict(request)
        binding = {
            "effect_id": new_id("eff"),
            "canonical_args_hash": canonical_effect_hash(context),
            "target_state_version": context.get("target_state_version"),
        }
        selected["effect_binding"] = binding
        once = selected.get("requested_once_capability")
        if isinstance(once, dict):
            constrained = dict(once)
            constraints = dict(constrained.get("constraints") or {})
            constraints[CapabilityManager.APPROVAL_BINDING_KEY] = binding
            constrained["constraints"] = constraints
            selected["requested_once_capability"] = constrained
        return selected

    def _permission_request_payload(self, pid: str, resource: str, rights: list[str], reason: str) -> dict[str, Any]:
        pattern = self.capabilities.parse_resource_pattern(resource)
        try:
            normalized_rights = [CapabilityRight(str(right)).value for right in rights]
        except ValueError as exc:
            raise ValidationError(f"unknown capability right: {exc}") from exc
        if not normalized_rights:
            raise ValidationError("permission request must include at least one right")
        self._reject_broad_model_permission_request(pattern.raw, pattern.kind, pattern.body, pattern.scope.value, normalized_rights)
        constraints = self._permission_constraints(pattern.kind, pattern.body, normalized_rights)
        risk = self._permission_risk(pattern.kind, normalized_rights, constraints)
        lease = {
            "type": "human_selected_policy",
            "choices": [
                CapabilityManager.ALWAYS_ALLOW,
                CapabilityManager.ASK_EACH_TIME,
                CapabilityManager.ALWAYS_DENY,
            ],
            "default_if_unanswered": CapabilityManager.ALWAYS_DENY,
            "expires_at": None,
            "uses_remaining": None,
        }
        context = {
            "reason": reason,
            "risk": risk.value,
            "resource": resource,
            "canonical_resource": pattern.raw,
            "resource_kind": pattern.kind,
            "resource_scope": pattern.scope.value,
            "resource_body": pattern.body,
            "rights": normalized_rights,
            "lease": lease,
            "constraints": constraints,
            "request_origin": "model",
        }
        return {
            "type": "permission_request",
            "question": f"Set permission policy for {pattern.raw} rights={normalized_rights}: {reason}",
            "requested_permission": {
                "subject": pid,
                "resource": pattern.raw,
                "rights": normalized_rights,
                "constraints": constraints,
            },
            "context": context,
        }

    def _reject_broad_model_permission_request(
        self,
        resource: str,
        kind: str,
        body: str,
        scope: str,
        rights: list[str],
    ) -> None:
        rights_set = set(rights)
        privileged_rights = {
            CapabilityRight.ADMIN.value,
            CapabilityRight.GRANT.value,
            CapabilityRight.REVOKE.value,
            CapabilityRight.WRITE.value,
            CapabilityRight.EXECUTE.value,
            CapabilityRight.DELETE.value,
        }
        if rights_set & privileged_rights:
            if kind == "capability" or (scope == "prefix" and not body):
                raise ValidationError(
                    "model permission requests cannot ask for broad privileged capability authority; request a concrete non-meta resource instead"
                )
        if kind == "shell" and CapabilityRight.EXECUTE.value in rights_set:
            if resource == self.config.shell.policy_resource or (scope == "prefix" and not body):
                raise ValidationError(
                    "model permission requests cannot ask for broad shell execute authority; request a concrete command class instead"
                )
        if kind == "filesystem" and rights_set & {CapabilityRight.WRITE.value, CapabilityRight.DELETE.value}:
            if resource == "filesystem:*" or body in {"", "/"}:
                raise ValidationError(
                    "model permission requests cannot ask for root/global filesystem write/delete authority; request a workspace, concrete file, or directory subtree"
                )
        if kind == "filesystem" and CapabilityRight.DELETE.value in rights_set:
            if resource == "filesystem:workspace:*" or (scope == "prefix" and body == "workspace"):
                raise ValidationError(
                    "model permission requests cannot ask for workspace-wide delete authority; request a concrete file or directory subtree"
                )

    def _permission_constraints(self, kind: str, body: str, rights: list[str]) -> dict[str, Any]:
        if kind != "shell" or CapabilityRight.EXECUTE.value not in set(rights):
            return {}
        command = body.split(":", 1)[0].strip().casefold()
        if command == "git":
            return {AUTHORITY_RULES_KEY: self._git_read_only_authority_rules()}
        raise ValidationError(
            f"model permission requests for shell:{command} must be approved through an exact per-use shell operation"
        )

    def _permission_risk(self, kind: str, rights: list[str], constraints: dict[str, Any]) -> AuthorityRisk:
        rights_set = set(rights)
        if CapabilityRight.DELETE.value in rights_set:
            return AuthorityRisk.DESTRUCTIVE
        if rights_set & {CapabilityRight.ADMIN.value, CapabilityRight.GRANT.value, CapabilityRight.REVOKE.value}:
            return AuthorityRisk.HIGH
        if kind == "shell" and CapabilityRight.EXECUTE.value in rights_set:
            return AuthorityRisk.LOW if constraints else AuthorityRisk.HIGH
        if CapabilityRight.WRITE.value in rights_set or CapabilityRight.EXECUTE.value in rights_set:
            return AuthorityRisk.HIGH
        return AuthorityRisk.LOW

    def _git_read_only_authority_rules(self) -> list[dict[str, Any]]:
        rules: list[dict[str, Any]] = []
        for subcommand in [
            "push",
            "clean",
            "reset",
            "checkout",
            "switch",
            "restore",
            "commit",
            "merge",
            "rebase",
            "tag",
            "remote",
            "fetch",
            "pull",
            "clone",
        ]:
            rules.append(
                {
                    "rule_id": f"shell.git.deny.{subcommand}",
                    "operation": "shell.run",
                    "effect": CapabilityEffect.DENY.value,
                    "risk": AuthorityRisk.HIGH.value,
                    "conditions": {"argv": ["git", subcommand], "match": "prefix"},
                    "description": f"deny git {subcommand} from read-only git command authority",
                }
            )
        for argv in [
            ["git", "status"],
            ["git", "status", "--short"],
            ["git", "branch", "--show-current"],
            ["git", "rev-parse", "--show-toplevel"],
            ["git", "diff"],
            ["git", "diff", "--stat"],
        ]:
            rules.append(
                {
                    "rule_id": f"shell.git.allow.{'.'.join(argv[1:])}",
                    "operation": "shell.run",
                    "effect": CapabilityEffect.ALLOW.value,
                    "risk": AuthorityRisk.LOW.value,
                    "conditions": {"argv": argv, "match": "exact"},
                    "description": "allow read-only git inspection command",
                }
            )
        return rules

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
        decision = self.capabilities.require(pid, resource, CapabilityRight.WRITE, consume=False)
        reservation_id = self._reserve_one_time_decision(decision, used_by="human")
        try:
            request_id = self.query(
                pid=pid,
                human=selected_human,
                request={
                    "type": "question",
                    "question": question,
                    "context": context or {},
                },
                blocking=blocking,
            )
        except Exception:
            self._restore_one_time_decision(reservation_id)
            raise
        self._commit_one_time_decision(reservation_id)
        return request_id

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
        selected_decision: Any = {"approved": True} if decision is None else decision
        if not isinstance(selected_decision, dict):
            raise ValidationError("human decision must be a JSON object")
        return self._decide(
            request_id,
            HumanRequestStatus.APPROVED,
            dict(selected_decision),
            responder or self.config.runtime.default_human_actor,
        )

    def reject(
        self,
        request_id: str,
        decision: dict[str, Any] | None = None,
        responder: str | None = None,
    ) -> HumanRequest:
        selected_decision: Any = {"approved": False} if decision is None else decision
        if not isinstance(selected_decision, dict):
            raise ValidationError("human decision must be a JSON object")
        return self._decide(
            request_id,
            HumanRequestStatus.REJECTED,
            dict(selected_decision),
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
        if process.status in self.TERMINAL_PROCESS_STATUSES:
            self.cancel_pending_for_process(
                pid,
                actor="human",
                reason=(payload or {}).get("reason") or f"process interrupted with {sig.value}",
            )
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
        selected_channel = self._normalize_output_channel(channel)
        if len(message) > self.config.tools.human_output_max_chars:
            raise ValidationError(
                f"human output message exceeds max characters={self.config.tools.human_output_max_chars}"
            )
        resource = f"human:{selected_human}"
        decision = self.capabilities.require(pid, resource, CapabilityRight.WRITE, consume=False)
        reservation_id = self._reserve_one_time_decision(decision, used_by="human")
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
        # Claim the just-inserted row against terminal queue drains, then cross
        # the potentially blocking provider boundary without holding the
        # terminal lock. Exit/cancel can proceed and a late delivery rechecks
        # the durable pending state.
        with self._terminal_lock:
            try:
                self.store.insert_human_request(request)
                operations = getattr(self.store, "operation_manager", None)
                if operations is not None:
                    operations.expect("approval")
                    operations.link_evidence(
                        "human_request",
                        request.request_id,
                        "approval",
                        metadata={"status": request.status.value, "blocking": request.blocking},
                    )
            except Exception:
                self._restore_one_time_decision(reservation_id)
                raise
            self._terminal_claims.add(request.request_id)
        try:
            try:
                delivered = self._deliver_output_request(request)
            except Exception:
                latest = self.store.get_human_request(request.request_id)
                if latest is None or latest.status == HumanRequestStatus.PENDING:
                    # A pre-effect failure must not leave a retryable pending
                    # output after returning its authority to the subject: a
                    # later terminal drain could otherwise deliver it after the
                    # restored one-shot grant had been spent elsewhere.
                    if latest is not None:
                        latest.status = HumanRequestStatus.CANCELLED
                        latest.decision = {"delivery_committed": False, "cancelled_before_delivery": True}
                        latest.updated_at = utc_now()
                        try:
                            self.store.update_human_request(latest)
                        except Exception:
                            self._commit_one_time_decision(reservation_id)
                            raise
                    self._restore_one_time_decision(reservation_id)
                else:
                    # The durable effect intent was committed before provider
                    # invocation; a provider exception is therefore ambiguous
                    # and must not resurrect one-shot authority.
                    self._commit_one_time_decision(reservation_id)
                raise
            self._commit_one_time_decision(reservation_id)
        finally:
            with self._terminal_lock:
                self._terminal_claims.discard(request.request_id)
        return {
            "delivered": True,
            "request_id": delivered.request_id,
            "channel": selected_channel,
            "chars": len(message),
        }

    def _normalize_output_channel(self, channel: str | None) -> str:
        selected = (channel or self.config.runtime.terminal_channel).strip()
        if not selected:
            raise ValidationError("human output channel must be non-empty")
        if len(selected) > 128:
            raise ValidationError("human output channel is too long")
        return selected

    def get(self, request_id: str) -> HumanRequest:
        request = self.store.get_human_request(request_id)
        if request is None:
            raise NotFound(f"human request not found: {request_id}")
        return request

    def list(self, pid: str | None = None) -> builtins.list[HumanRequest]:
        # Pending decisions are liveness-critical and must never fall behind a
        # bounded history window.  Put every pending request first, followed by
        # the newest historical window for observability.
        pending = self.store.list_human_requests(pid=pid, status=HumanRequestStatus.PENDING)
        recent = self.store.list_human_requests(
            pid=pid,
            limit=self.config.tools.human_request_list_limit,
            newest=True,
        )
        pending_ids = {request.request_id for request in pending}
        return [*pending, *(request for request in recent if request.request_id not in pending_ids)]

    def pending(self, human: str | None = None) -> builtins.list[HumanRequest]:
        return self.store.list_human_requests(
            human=human,
            status=HumanRequestStatus.PENDING,
            limit=self.config.tools.human_request_list_limit,
        )

    def cancel_pending_for_process(self, pid: str, *, actor: str, reason: str) -> builtins.list[str]:
        """Cancel every pending request owned by a terminal process."""
        cancelled: builtins.list[str] = []
        with self._terminal_lock:
            with self.store.transaction():
                for request in self.store.list_human_requests(
                    pid=pid,
                    status=HumanRequestStatus.PENDING,
                ):
                    request.status = HumanRequestStatus.CANCELLED
                    request.decision = {"cancelled_by": actor, "reason": reason}
                    request.updated_at = utc_now()
                    self.store.update_human_request(request)
                    cancelled.append(request.request_id)
                    self.events.emit(
                        EventType.HUMAN_RESPONSE,
                        source=actor,
                        target=pid,
                        payload={
                            "request_id": request.request_id,
                            "status": HumanRequestStatus.CANCELLED.value,
                            "reason": reason,
                        },
                    )
                    self.audit.record(
                        actor=actor,
                        action="human.request_cancelled",
                        target=f"human_request:{request.request_id}",
                        decision={"pid": pid, "reason": reason},
                    )
        return cancelled

    def process_next_terminal(
        self,
        human: str | None = None,
        auto_approve: bool | None = None,
        auto_policy: str | None = None,
        auto_answer: str | None = None,
    ) -> HumanRequest | None:
        selected_human = human or self.config.runtime.default_human
        with self._terminal_lock:
            pending = self.pending(human=selected_human)
            if not pending:
                return None
            # The terminal is the human's message queue. Process requests strictly
            # in creation order so approvals and answers remain predictable. A
            # second drain may not skip a claimed head request.
            request = pending[0]
            if request.request_id in self._terminal_claims:
                return None
            self._terminal_claims.add(request.request_id)
        try:
            return self._process_claimed_terminal_request(
                request=request,
                auto_approve=auto_approve,
                auto_policy=auto_policy,
                auto_answer=auto_answer,
            )
        finally:
            with self._terminal_lock:
                self._terminal_claims.discard(request.request_id)

    def _process_claimed_terminal_request(
        self,
        *,
        request: HumanRequest,
        auto_approve: bool | None,
        auto_policy: str | None,
        auto_answer: str | None,
    ) -> HumanRequest:
        request_type = request.payload.get("type")
        if request_type == "output":
            return self._deliver_output_request(request)
        question = self._terminal_question(request)
        if request_type == "question":
            answer = self._select_text_answer(
                request=request,
                question=question,
                auto_answer=auto_answer,
            )
            return self.approve(
                request.request_id,
                {"approved": True, "answer": answer, "source": "terminal_queue"},
            )
        if request_type == "permission_request":
            policy = self._select_permission_policy(
                request=request,
                question=question,
                auto_policy=auto_policy,
                auto_approve=auto_approve,
            )
            decision = {"policy": policy, "source": "terminal_queue"}
            if policy == CapabilityManager.ALWAYS_DENY:
                return self.reject(request.request_id, {"approved": False, **decision})
            return self.approve(request.request_id, {"approved": True, **decision})

        approved = self._select_boolean_approval(
            request=request,
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
        operations = getattr(self.store, "operation_manager", None)
        if operations is not None:
            candidates = operations.operation_for_evidence(("human_request",), request_id)
            if len(candidates) == 1:
                with operations.attach(candidates[0].operation_id):
                    return self._decide_impl(request_id, status, decision, responder)
        return self._decide_impl(request_id, status, decision, responder)

    def _decide_impl(
        self,
        request_id: str,
        status: HumanRequestStatus,
        decision: dict[str, Any],
        responder: str,
    ) -> HumanRequest:
        with self._terminal_lock:
            with self.store.transaction():
                request = self.store.get_human_request(request_id)
                if request is None:
                    raise NotFound(f"human request not found: {request_id}")
                if request.status != HumanRequestStatus.PENDING:
                    raise ValidationError(f"human request is not pending: {request_id} status={request.status.value}")
                process = self.store.get_process(request.pid)
                if process is not None and process.status in self.TERMINAL_PROCESS_STATUSES:
                    raise ValidationError(
                        f"terminal process cannot receive a human decision: {request.pid} status={process.status.value}"
                    )
                self._validate_decision_side_effects(request, status, decision)
                self._apply_decision_side_effects(request, status, decision, responder)
                permission_related = False
                permission_spec = request.payload.get("requested_permission")
                if isinstance(permission_spec, dict):
                    permission_related = True

                once_spec = request.payload.get("requested_once_capability")
                if isinstance(once_spec, dict):
                    permission_related = True
                request.status = status
                request.decision = decision
                request.updated_at = utc_now()
                self.store.update_human_request(request)
                if process is not None and process.status == ProcessStatus.WAITING_HUMAN:
                    remaining = [
                        pending
                        for pending in self.store.list_human_requests(
                            pid=request.pid,
                            status=HumanRequestStatus.PENDING,
                        )
                        if pending.blocking
                    ]
                    if remaining:
                        process.status = ProcessStatus.WAITING_HUMAN
                        process.status_message = "waiting for human requests " + ",".join(
                            pending.request_id for pending in remaining[:8]
                        )
                    else:
                        # Permission denials still wake the process so it can observe
                        # the structured failed operation. Generic rejections pause.
                        process.status = (
                            ProcessStatus.RUNNABLE
                            if status == HumanRequestStatus.APPROVED or permission_related
                            else ProcessStatus.PAUSED
                        )
                        process.status_message = (
                            None
                            if status == HumanRequestStatus.APPROVED
                            else f"human rejected {request_id}"
                        )
                    process.updated_at = utc_now()
                    self.store.update_process(process)
                self.events.emit(
                    EventType.HUMAN_RESPONSE,
                    source=responder,
                    target=request.pid,
                    payload={
                        "request_id": request_id,
                        "status": status.value,
                        "decision": _sanitize_human_observability(decision),
                    },
                )
                self.audit.record(
                    actor=responder,
                    action="human.response",
                    target=f"human_request:{request_id}",
                    decision={"status": status.value, "decision": _sanitize_human_observability(decision)},
                )
        return request

    def _validate_decision_side_effects(
        self,
        request: HumanRequest,
        status: HumanRequestStatus,
        decision: dict[str, Any],
    ) -> None:
        approved = decision.get("approved")
        expected_approved = status == HumanRequestStatus.APPROVED
        if not isinstance(approved, bool):
            raise ValidationError("human decision approved must be a JSON boolean")
        if approved is not expected_approved:
            raise ValidationError(
                f"human decision approved={approved} conflicts with status={status.value}"
            )
        request_type = request.payload.get("type")
        if request_type == "question" and status == HumanRequestStatus.APPROVED:
            if "answer" not in decision:
                raise ValidationError("approved human question requires an answer")
            if not isinstance(decision["answer"], str):
                raise ValidationError("human question answer must be a string")
            if not decision["answer"].strip():
                raise ValidationError("human question answer must be non-empty")
        permission_spec = request.payload.get("requested_permission")
        if isinstance(permission_spec, dict):
            self._permission_decision_spec(permission_spec, request.pid, status, decision)
        once_spec = request.payload.get("requested_once_capability")
        if isinstance(once_spec, dict) and status == HumanRequestStatus.APPROVED:
            self._capability_request_spec(once_spec, request.pid, label="requested one-time capability")
        cap_spec = request.payload.get("requested_capability")
        if isinstance(cap_spec, dict) and status == HumanRequestStatus.APPROVED:
            self._capability_request_spec(cap_spec, request.pid, label="requested capability")

    def _apply_decision_side_effects(
        self,
        request: HumanRequest,
        status: HumanRequestStatus,
        decision: dict[str, Any],
        responder: str,
    ) -> None:
        permission_spec = request.payload.get("requested_permission")
        if isinstance(permission_spec, dict):
            subject, resource, rights, constraints, policy = self._permission_decision_spec(
                permission_spec,
                request.pid,
                status,
                decision,
            )
            self.capabilities.set_permission_policy(
                subject=subject,
                resource=resource,
                rights=rights,
                policy=policy,
                issued_by=responder,
                constraints=constraints,
            )

        once_spec = request.payload.get("requested_once_capability")
        if isinstance(once_spec, dict) and status == HumanRequestStatus.APPROVED:
            subject, resource, rights, constraints, _expires_at, _delegable = self._capability_request_spec(
                once_spec,
                request.pid,
                label="requested one-time capability",
            )
            self.capabilities.grant_once(
                subject=subject,
                resource=resource,
                rights=rights,
                issued_by=responder,
                constraints=constraints,
            )

        cap_spec = request.payload.get("requested_capability")
        if isinstance(cap_spec, dict) and status == HumanRequestStatus.APPROVED:
            subject, resource, rights, constraints, expires_at, delegable = self._capability_request_spec(
                cap_spec,
                request.pid,
                label="requested capability",
            )
            self.capabilities.grant(
                subject=subject,
                resource=resource,
                rights=rights,
                issued_by=responder,
                constraints=constraints,
                expires_at=expires_at,
                delegable=delegable,
            )

    def _permission_decision_spec(
        self,
        spec: dict[str, Any],
        default_subject: str,
        status: HumanRequestStatus,
        decision: dict[str, Any],
    ) -> tuple[str, str, list[str], dict[str, Any] | None, str]:
        subject, resource, rights, constraints, _expires_at, _delegable = self._capability_request_spec(
            spec,
            default_subject,
            label="requested permission",
        )
        policy_value = decision.get("policy")
        if not isinstance(policy_value, str):
            raise ValidationError("permission decisions require an explicit policy")
        policy = policy_value
        if policy not in {
            CapabilityManager.ALWAYS_ALLOW,
            CapabilityManager.ALWAYS_DENY,
            CapabilityManager.ASK_EACH_TIME,
        }:
            raise ValidationError(f"unknown permission policy: {policy}")
        if status == HumanRequestStatus.REJECTED and policy == CapabilityManager.ALWAYS_ALLOW:
            raise ValidationError("rejected permission requests cannot install always_allow policy")
        if status == HumanRequestStatus.APPROVED and policy == CapabilityManager.ALWAYS_DENY:
            raise ValidationError("approved permission requests cannot install always_deny policy")
        return subject, resource, rights, constraints, policy

    def _capability_request_spec(
        self,
        spec: dict[str, Any],
        default_subject: str,
        *,
        label: str,
    ) -> tuple[str, str, list[str], dict[str, Any] | None, str | None, bool]:
        resource = spec.get("resource")
        if not isinstance(resource, str):
            raise ValidationError(f"{label} must include a string resource")
        subject = spec.get("subject", default_subject)
        if not isinstance(subject, str):
            subject = default_subject
        rights = spec.get("rights", ["execute"])
        if not isinstance(rights, list):
            rights = ["execute"]
        normalized_rights = [str(right) for right in rights]
        constraints = spec.get("constraints")
        expires_at = spec.get("expires_at")
        return (
            subject,
            resource,
            normalized_rights,
            constraints if isinstance(constraints, dict) else None,
            expires_at if isinstance(expires_at, str) else None,
            bool(spec.get("delegable", False)),
        )

    def _reserve_one_time_decision(self, decision: Any, *, used_by: str) -> str | None:
        return self.capabilities.reserve_decision_use(
            decision,
            used_by=used_by,
            reason="one-time human permission reserved",
        )

    def _commit_one_time_decision(self, reservation_id: str | None) -> None:
        self.capabilities.commit_reserved_use(
            reservation_id,
            committed_by="human",
            reason="one-time human permission committed",
        )

    def _restore_one_time_decision(self, reservation_id: str | None) -> None:
        self.capabilities._restore_reserved_use(
            reservation_id,
            restored_by="human",
            reason="one-time human permission restored before request commit",
        )

    def _select_permission_policy(
        self,
        request: HumanRequest,
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
            self._terminal_provider_io(
                request,
                operation="write",
                text=f"{question} [policy={auto_policy}]",
                purpose="permission_policy_auto",
            )
            return auto_policy
        if auto_approve is not None:
            policy = CapabilityManager.ALWAYS_ALLOW if auto_approve else CapabilityManager.ALWAYS_DENY
            self._terminal_provider_io(
                request,
                operation="write",
                text=f"{question} [policy={policy}]",
                purpose="permission_policy_auto",
            )
            return policy
        answer = str(
            self._terminal_provider_io(
                request,
                operation="read",
                text=f"{question} [a=always allow, d=always deny, e=ask each time; default=d]: ",
                purpose="permission_policy",
            )
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

    def format_terminal_request(self, request: HumanRequest) -> str:
        return self._terminal_question(request)

    def _terminal_question(self, request: HumanRequest) -> str:
        question = str(request.payload.get("question") or request.payload)
        if request.payload.get("type") == "permission_request":
            return self._permission_terminal_question(request, question)
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
            ("risk", "risk"),
            ("rule id", "rule_id"),
            ("rule effect", "rule_effect"),
        ]:
            if key in context:
                lines.append(f"- {label}: {context[key]}")
        profile = context.get("sandbox_profile")
        if isinstance(profile, dict):
            lines.append("- sandbox profile:")
            for key in ["operation", "resource", "effect", "risk", "rule_id"]:
                if key in profile:
                    lines.append(f"  - {key}: {profile[key]}")
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

    def _permission_terminal_question(self, request: HumanRequest, question: str) -> str:
        context = request.payload.get("context")
        permission = request.payload.get("requested_permission")
        if not isinstance(context, dict):
            return question
        lines = ["Permission request details:"]
        for label, key in [
            ("process", "pid"),
            ("reason", "reason"),
            ("risk", "risk"),
            ("requested resource", "resource"),
            ("canonical resource", "canonical_resource"),
            ("resource kind", "resource_kind"),
            ("resource scope", "resource_scope"),
            ("resource body", "resource_body"),
            ("rights", "rights"),
            ("origin", "request_origin"),
        ]:
            value = request.pid if key == "pid" else context.get(key)
            if value is not None:
                lines.append(f"- {label}: {value}")
        lease = context.get("lease")
        if isinstance(lease, dict):
            lines.append("- lease:")
            for key in ["type", "choices", "default_if_unanswered", "expires_at", "uses_remaining"]:
                if key in lease:
                    lines.append(f"  - {key}: {lease[key]}")
        constraints = context.get("constraints")
        if isinstance(constraints, dict):
            lines.append("- constraints:")
            rules = constraints.get(AUTHORITY_RULES_KEY)
            if isinstance(rules, list) and rules:
                lines.append("  - authority_rules:")
                for rule in rules:
                    if not isinstance(rule, dict):
                        continue
                    lines.append(
                        "    - "
                        f"{rule.get('rule_id')} "
                        f"effect={rule.get('effect')} "
                        f"risk={rule.get('risk')} "
                        f"conditions={rule.get('conditions')}"
                    )
            elif constraints:
                for key in sorted(constraints):
                    lines.append(f"  - {key}: {constraints[key]}")
            else:
                lines.append("  - <none>")
        if isinstance(permission, dict):
            lines.append("- requested policy target:")
            lines.append(f"  - resource: {permission.get('resource')}")
            lines.append(f"  - rights: {permission.get('rights')}")
        lines.append(question)
        return "\n".join(lines)

    def _indent_block(self, text: str) -> str:
        if not text:
            return "  <empty>"
        return "\n".join(f"  {line}" for line in text.splitlines() or [text])

    def _terminal_provider_io(
        self,
        request: HumanRequest,
        *,
        operation: str,
        text: str,
        purpose: str,
    ) -> str | None:
        operations = getattr(self.store, "operation_manager", None)
        if operations is not None:
            candidates = operations.operation_for_evidence(("human_request",), request.request_id)
            if len(candidates) == 1:
                with operations.attach(candidates[0].operation_id):
                    return self._terminal_provider_io_impl(
                        request,
                        operation=operation,
                        text=text,
                        purpose=purpose,
                    )
        return self._terminal_provider_io_impl(
            request,
            operation=operation,
            text=text,
            purpose=purpose,
        )

    def _terminal_provider_io_impl(
        self,
        request: HumanRequest,
        *,
        operation: str,
        text: str,
        purpose: str,
    ) -> str | None:
        if operation not in {"read", "write"}:
            raise ValidationError(f"unsupported terminal human provider operation: {operation}")
        require_external_effect_classifier(self.provider, operation)
        resource = f"human:{request.human}"
        prompt_observation = self._terminal_text_observation(text)
        request_kind = (
            "approval"
            if purpose.startswith(("permission_policy", "boolean_approval"))
            else "question"
            if purpose.startswith("text_answer")
            else purpose
        )
        effect_context = {
            "request_id": request.request_id,
            "request_kind": request_kind,
            "purpose": purpose,
            "operation": operation,
            "chars": prompt_observation["chars"],
            "prompt_observation": prompt_observation,
        }
        effect_intent = begin_external_effect_intent(
            self.store,
            pid=request.pid,
            provider="human",
            operation=operation,
            target=resource,
            state_mutation=operation == "write",
            information_flow=True,
            metadata={"context": effect_context},
        )
        try:
            result = self.provider.read(text) if operation == "read" else self.provider.write(text)
        except ProviderEffectNotStarted:
            with self.store.transaction():
                abandon_external_effect_intent(self.store, effect_intent.effect_id)
            raise
        except BaseException as exc:
            # The provider may have emitted the prompt or accepted input before
            # failing. Preserve UNKNOWN evidence, but never persist prompt,
            # answer, or exception text from this sensitive boundary.
            try:
                self._finalize_terminal_provider_failure(
                    request,
                    operation=operation,
                    resource=resource,
                    purpose=purpose,
                    effect_context=effect_context,
                    effect_intent_id=effect_intent.effect_id,
                    error=exc,
                )
            except Exception:
                pass
            raise

        result_observation = (
            self._terminal_text_observation(result)
            if isinstance(result, str)
            else {"type": type(result).__name__}
        )
        try:
            event = self.events.emit(
                EventType.HUMAN_RESPONSE if operation == "read" else EventType.HUMAN_OUTPUT,
                source=request.human if operation == "read" else request.pid,
                target=resource,
                payload={
                    "request_id": request.request_id,
                    "purpose": purpose,
                    "operation": operation,
                    "chars": result_observation.get("chars", prompt_observation["chars"]),
                },
            )
            audit_record = self.audit.record(
                actor=request.pid,
                action=f"human.terminal.{operation}",
                target=resource,
                decision={
                    "request_id": request.request_id,
                    "purpose": purpose,
                    "operation": operation,
                    "prompt_observation": prompt_observation,
                    "result_observation": result_observation,
                },
            )
            classification = classify_external_effect(
                self.provider,
                operation,
                effect_context,
                {"completed": True, "result_observation": result_observation},
            )
            if not classification.information_flow:
                classification = ExternalEffectClassification(
                    rollback_class=classification.rollback_class,
                    rollback_status=classification.rollback_status,
                    state_mutation=classification.state_mutation,
                    information_flow=True,
                    metadata={**classification.metadata, "terminal_information_flow": True},
                )
            record_external_effect(
                self.store,
                pid=request.pid,
                provider="human",
                operation=operation,
                target=resource,
                classification=classification,
                audit_record=audit_record,
                event=event,
                metadata={
                    "context": effect_context,
                    "result_observation": result_observation,
                },
                intent_effect_id=effect_intent.effect_id,
            )
        except Exception:
            # The human interaction has already completed. Leave the durable
            # pending intent and continue committing the chosen answer/policy
            # so queue draining cannot repeat the prompt.
            pass
        return result

    def _finalize_terminal_provider_failure(
        self,
        request: HumanRequest,
        *,
        operation: str,
        resource: str,
        purpose: str,
        effect_context: dict[str, Any],
        effect_intent_id: str,
        error: BaseException,
    ) -> None:
        event = self.events.emit(
            EventType.HUMAN_RESPONSE if operation == "read" else EventType.HUMAN_OUTPUT,
            source=request.human if operation == "read" else request.pid,
            target=resource,
            payload={
                "request_id": request.request_id,
                "purpose": purpose,
                "operation": operation,
                "outcome": "unknown",
                "error_type": type(error).__name__,
            },
        )
        audit_record = self.audit.record(
            actor=request.pid,
            action=f"human.terminal.{operation}.failed",
            target=resource,
            decision={
                "request_id": request.request_id,
                "purpose": purpose,
                "operation": operation,
                "effect_outcome": "unknown",
                "error_type": type(error).__name__,
            },
        )
        record_external_effect(
            self.store,
            pid=request.pid,
            provider="human",
            operation=operation,
            target=resource,
            classification=ExternalEffectClassification(
                rollback_class=ExternalEffectRollbackClass.UNKNOWN,
                rollback_status=ExternalEffectRollbackStatus.UNKNOWN,
                state_mutation=operation == "write",
                information_flow=True,
                metadata={"outcome": "unknown_after_provider_exception"},
            ),
            audit_record=audit_record,
            event=event,
            metadata={
                "context": effect_context,
                "error_type": type(error).__name__,
            },
            intent_effect_id=effect_intent_id,
        )

    def _terminal_text_observation(self, text: str) -> dict[str, Any]:
        encoded = text.encode("utf-8")
        return {
            "chars": len(text),
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }

    def _select_boolean_approval(
        self,
        request: HumanRequest,
        question: str,
        auto_approve: bool | None,
    ) -> bool:
        if auto_approve is None:
            answer = str(
                self._terminal_provider_io(
                    request,
                    operation="read",
                    text=f"{question} [y/N]: ",
                    purpose="boolean_approval",
                )
            ).strip().lower()
            return answer in {"y", "yes"}
        self._terminal_provider_io(
            request,
            operation="write",
            text=f"{question} [{'approved' if auto_approve else 'rejected'}]",
            purpose="boolean_approval_auto",
        )
        return auto_approve

    def _select_text_answer(
        self,
        request: HumanRequest,
        question: str,
        auto_answer: str | None,
    ) -> str:
        if auto_answer is not None:
            self._terminal_provider_io(
                request,
                operation="write",
                text=f"{question} [answer={auto_answer!r}]",
                purpose="text_answer_auto",
            )
            return auto_answer
        return str(
            self._terminal_provider_io(
                request,
                operation="read",
                text=f"{question} ",
                purpose="text_answer",
            )
        )

    def _deliver_output_request(self, request: HumanRequest) -> HumanRequest:
        message = str(request.payload.get("message", ""))
        channel = str(request.payload.get("channel", self.config.runtime.terminal_channel))
        effect_context = {
            "channel": channel,
            "chars": len(message),
            "request_id": request.request_id,
            "request_kind": "output",
        }
        require_external_effect_classifier(self.provider, "write")
        resource = f"human:{request.human}"
        with self.store.transaction():
            latest = self.store.get_human_request(request.request_id)
            if latest is None:
                raise NotFound(f"human request not found: {request.request_id}")
            if latest.status != HumanRequestStatus.PENDING:
                raise ValidationError(
                    f"human output request is not pending: {request.request_id} status={latest.status.value}"
                )
            process = self.store.get_process(latest.pid)
            if process is not None and process.status in self.TERMINAL_PROCESS_STATUSES:
                raise ValidationError(
                    f"terminal process cannot deliver human output: {latest.pid} status={process.status.value}"
                )
            request = latest
            request.status = HumanRequestStatus.DELIVERED
            request.decision = {"delivery_committed": True}
            request.updated_at = utc_now()
            self.store.update_human_request(request)
            event = self.events.emit(
                EventType.HUMAN_OUTPUT,
                source=request.pid,
                target=resource,
                payload={"request_id": request.request_id, "channel": channel, "chars": len(message)},
            )
            audit_record = self.audit.record(
                actor=request.pid,
                action="human.output",
                target=resource,
                decision={
                    "request_id": request.request_id,
                    "channel": channel,
                    "chars": len(message),
                    "delivery_committed": True,
                },
            )
            effect_intent = begin_external_effect_intent(
                self.store,
                pid=request.pid,
                provider="human",
                operation="write",
                target=resource,
                state_mutation=True,
                information_flow=True,
                metadata={"context": effect_context, "result": {"delivery_committed": True}},
            )
        try:
            self.provider.write(message)
        except ProviderEffectNotStarted:
            with self.store.transaction():
                abandon_external_effect_intent(self.store, effect_intent.effect_id)
                latest = self.store.get_human_request(request.request_id)
                if latest is not None and latest.status == HumanRequestStatus.DELIVERED:
                    latest.status = HumanRequestStatus.PENDING
                    latest.decision = {
                        "delivery_committed": False,
                        "provider_not_started": True,
                    }
                    latest.updated_at = utc_now()
                    self.store.update_human_request(latest)
                self.audit.record(
                    actor=request.pid,
                    action="human.output.not_started",
                    target=resource,
                    decision={
                        "request_id": request.request_id,
                        "channel": channel,
                        "chars": len(message),
                        "provider_started": False,
                    },
                )
            raise
        except Exception as exc:
            try:
                record_external_effect(
                    self.store,
                    pid=request.pid,
                    provider="human",
                    operation="write",
                    target=resource,
                    classification=ExternalEffectClassification(
                        rollback_class=ExternalEffectRollbackClass.UNKNOWN,
                        rollback_status=ExternalEffectRollbackStatus.UNKNOWN,
                        state_mutation=True,
                        information_flow=True,
                        metadata={"outcome": "unknown_after_provider_exception"},
                    ),
                    audit_record=audit_record,
                    event=event,
                    metadata={
                        "context": effect_context,
                        "error_type": type(exc).__name__,
                    },
                    intent_effect_id=effect_intent.effect_id,
                )
            except Exception:
                # The pre-provider pending intent remains durable.
                pass
            request.decision = {
                "delivery_committed": True,
                # Provider exceptions can echo terminal payloads or transport
                # details.  Persist only the stable error class, matching the
                # interactive terminal path's privacy boundary.
                "provider_error_type": type(exc).__name__,
            }
            request.updated_at = utc_now()
            try:
                self.store.update_human_request(request)
            except Exception:
                pass
            raise
        try:
            classification = classify_external_effect(
                self.provider,
                "write",
                effect_context,
                {"delivery_committed": True, "delivered": True},
            )
            record_external_effect(
                self.store,
                pid=request.pid,
                provider="human",
                operation="write",
                target=resource,
                classification=classification,
                audit_record=audit_record,
                event=event,
                metadata={
                    "context": effect_context,
                    "result": {"delivery_committed": True, "delivered": True},
                },
                intent_effect_id=effect_intent.effect_id,
            )
        except Exception:
            # Delivery is at-most-once and already happened.  Do not surface a
            # post-provider bookkeeping failure as retryable; the UNKNOWN
            # pending intent is the fail-closed evidence.
            pass
        request.decision = {"delivery_committed": True, "delivered": True}
        request.updated_at = utc_now()
        try:
            self.store.update_human_request(request)
        except Exception:
            pass
        return request

    def _default_message_subject(self, kind: ProcessMessageKind) -> str:
        if kind == ProcessMessageKind.INTERRUPT:
            return "Human interrupt"
        return "Human message"

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
from dataclasses import replace
from typing import Any, TYPE_CHECKING

from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models.exceptions import (
    HumanApprovalRequired,
    NotFound,
    ProcessMessageWaitRequired,
    ProcessWaitRequired,
    ResourceLimitExceeded,
)
from agent_libos.utils.ids import new_id, utc_now
from agent_libos.utils.serde import dumps, to_jsonable
from agent_libos.llm.action_parser import parse_json_action
from agent_libos.llm.client import LLMClient
from agent_libos.llm.context_memory import LLMContextMemory
from agent_libos.llm.prompt import build_system_prompt, build_user_prompt
from agent_libos.llm.records import observable_llm_call_fields
from agent_libos.llm.tool_protocol import tool_call_to_action
from agent_libos.tools.observability import sanitize_for_observability
from agent_libos.models import (
    DataFlowContext,
    DataSink,
    EventType,
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    HumanRequestStatus,
    LLMCallRecord,
    ObjectHandle,
    ObjectRight,
    ProcessMessageKind,
    ProcessStatus,
    ResourceUsage,
    ViewMode,
)
from agent_libos.sdk import (
    ProtectedOperationEvidence,
    ProtectedOperationInvocation,
    ProviderPhase,
)
from agent_libos.substrate import ProviderEffectNotStarted

if TYPE_CHECKING:
    from agent_libos.runtime.runtime import Runtime


class _LLMProviderChainScopeChanged(ProviderEffectNotStarted):
    """The selected provider-side state no longer matches the dispatch scope."""


class _LLMReleaseApprovalRequired(HumanApprovalRequired):
    """A conditional LLM request whose exact prepared payload must be resumed."""

    def __init__(
        self,
        original: HumanApprovalRequired,
        prepared_request: dict[str, Any],
    ) -> None:
        super().__init__(original.request_id, str(original))
        self.prepared_request = prepared_request


class _LLMReleasePayloadUnavailable(RuntimeError):
    """An opt-out release cannot be resumed after its in-memory payload is lost."""


class LLMProcessExecutor:
    """Runs one model-selected tool action per process quantum."""

    def __init__(self, runtime: "Runtime", client: LLMClient | None = None, config: AgentLibOSConfig | None = None):
        self.runtime = runtime
        self.config = config or DEFAULT_CONFIG
        if client is not None:
            self.runtime.llms.set_test_client(self.config.llm.default_profile_id, client)
        # Pending actions are held outside Object Memory because the process has
        # not received a tool result yet. The action is retried after the human
        # queue records a decision, without asking the model for a new action.
        self._pending_human_actions: dict[str, dict[str, Any]] = {}
        self._pending_llm_release_actions: dict[str, dict[str, Any]] = {}
        self._pending_wait_actions: dict[str, dict[str, Any]] = {}
        self._pending_message_actions: dict[str, dict[str, Any]] = {}
        self.context_memory = LLMContextMemory(runtime)
        self._load_pending_actions()

    @property
    def client(self) -> Any:
        """Compatibility view of the default LLM profile client."""
        return self.runtime.llms.default_client

    @client.setter
    def client(self, value: Any) -> None:
        self.runtime.llms.set_test_client(self.config.llm.default_profile_id, value)

    def run_once(self, pid: str) -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.arun_once(pid))
        raise RuntimeError("Cannot call run_once() inside a running event loop. Use await arun_once(...).")

    async def arun_once(self, pid: str) -> dict[str, Any]:
        process = self.runtime.process.get(pid)
        if process.status not in {ProcessStatus.RUNNING, ProcessStatus.RUNNABLE}:
            return await self._arun_once_impl(pid)
        pending = self.runtime.store.get_llm_pending_action(pid)
        operation_id = str(pending.get("llm_operation_id") or "") if pending is not None else ""
        with self.runtime.operations.scope(
            kind="llm_request",
            name="llm.action_selection",
            actor=pid,
            pid=pid,
            expected_roles=["context", "invocation", "audit"],
            operation_id=operation_id or None,
            auto_finish=False,
        ) as operation:
            result = await self._arun_once_impl(pid)
            if any(result.get(key) for key in ("waiting_human", "waiting_event", "waiting_message", "pending_action_resuming")):
                self.runtime.operations.wait(operation_id=operation.operation_id)
            elif result.get("resource_limit_exceeded"):
                self.runtime.operations.finish("denied", operation_id=operation.operation_id)
            elif result.get("ok"):
                descendants = self.runtime.store.list_operations(
                    root_operation_id=operation.root_operation_id
                )
                outcome = (
                    "unknown"
                    if any(
                        candidate.operation_id != operation.operation_id
                        and candidate.outcome.value == "unknown"
                        for candidate in descendants
                    )
                    else "succeeded"
                )
                self.runtime.operations.finish(outcome, operation_id=operation.operation_id)
            elif result.get("skipped"):
                self.runtime.operations.finish("interrupted", operation_id=operation.operation_id)
            else:
                self.runtime.operations.finish("failed", operation_id=operation.operation_id)
            return result

    async def _arun_once_impl(self, pid: str) -> dict[str, Any]:
        process = self.runtime.process.get(pid)
        if process.status not in {ProcessStatus.RUNNING, ProcessStatus.RUNNABLE}:
            return {"ok": False, "skipped": True, "status": process.status.value}
        durable_pending = self._synchronize_pending_action(pid)
        if pid in self._pending_llm_release_actions:
            return await self._resume_pending_action_fail_closed(
                pid,
                self._resume_pending_llm_release_action,
            )
        if pid in self._pending_human_actions:
            return await self._resume_pending_action_fail_closed(pid, self._resume_pending_human_action)
        if pid in self._pending_wait_actions:
            return await self._resume_pending_action_fail_closed(pid, self._resume_pending_wait_action)
        if pid in self._pending_message_actions:
            return await self._resume_pending_action_fail_closed(pid, self._resume_pending_message_action)
        if durable_pending is not None and durable_pending.get("status") == "resuming":
            return {
                "ok": False,
                "pending_action_resuming": True,
                "wait_type": durable_pending.get("wait_type"),
            }
        image = self.runtime.images.get(process.image_id)
        if image is None:
            error = f"agent image not found for process {pid}: {process.image_id}"
            self.runtime.process.exit(pid, failed=True, message=error)
            self.runtime.audit.record(
                actor=pid,
                action="llm.image_missing",
                target=f"image:{process.image_id}",
                decision={"error": error},
            )
            return {"ok": False, "error": error}
        if process.memory_view is None:
            process.memory_view = self.runtime.memory.create_view(pid, [], mode=ViewMode.READ_ONLY)
            process.updated_at = utc_now()
            self.runtime.store.update_process(process)

        self._notify_interrupt_messages(pid)
        source_view = self.context_memory.view_without_context(pid, process.memory_view)
        source_context = self.runtime.memory.materialize_context(
            pid,
            source_view,
            policy=image.context_policy,
            budget_tokens=process.resource_budget.max_context_materialization_tokens,
            charge_resources=False,
        )
        events = [
            replace(
                event,
                source=self.runtime.tools.redact_model_context(pid, event.source),
                payload=self.runtime.tools.redact_model_context(pid, event.payload),
                correlation_id=self.runtime.tools.redact_model_context(pid, event.correlation_id),
                causality=self.runtime.tools.redact_model_context(pid, event.causality),
            )
            for event in self.runtime.events.list(
                target=pid,
                limit=self.runtime.config.llm_context.recent_event_limit,
                after_event_id=process.event_cursor,
            )
        ]
        capabilities = self.runtime.capability.capabilities_for(pid)
        # The prompt-visible tool list must match the process tool table. The
        # broker still owns the real execute check, but showing extra tools
        # teaches the model to choose actions the process cannot call.
        tools = self.runtime.tools.model_visible_tools(pid)
        prompt_process = replace(
            process,
            tool_table=self.runtime.tools.model_tool_table(pid),
            loaded_skills=self.runtime.tools.model_loaded_skills(pid),
        )
        skills = self.runtime.skills.prompt_context(pid)
        try:
            context = self.context_memory.prepare(
                pid=pid,
                image=image,
                process=prompt_process,
                source_context=source_context,
                events=events,
                capabilities=capabilities,
                tools=tools,
            )
        except ResourceLimitExceeded as exc:
            self.runtime.resources.kill_if_exceeded(pid, reason=str(exc))
            self.runtime.audit.record(
                actor=pid,
                action="llm.resource_limit_exceeded",
                target=f"process:{pid}",
                decision={"error": str(exc)},
            )
            return {"ok": False, "resource_limit_exceeded": True, "error": str(exc)}
        flow_context = self.runtime.data_flow.context_from_materialization(pid, context)
        if events:
            current = self.runtime.process.get(pid)
            if current.event_cursor != events[-1].event_id:
                current.event_cursor = events[-1].event_id
                current.updated_at = utc_now()
                self.runtime.store.update_process(current)
        messages = [
            {"role": "system", "content": build_system_prompt(image)},
            {
                "role": "user",
                "content": build_user_prompt(
                    process=prompt_process,
                    context=context,
                    events=events,
                    capabilities=capabilities,
                    tools=tools,
                    skills=skills,
                    prompt_mode=image.prompt_mode,
                ),
            },
        ]
        self.runtime.audit.record(
            actor=pid,
            action="llm.request",
            target=f"image:{image.image_id}",
            input_refs=context.object_refs,
            decision={"messages": len(messages), "policy": image.context_policy},
        )
        flow_token = self.runtime.data_flow.push(flow_context)
        try:
            openai_tools = self.runtime.tools.openai_tool_schemas(pid)
            response_scope_fingerprint = self._responses_state_scope_fingerprint(
                pid=pid,
                process=prompt_process,
                context=context,
                tools=openai_tools,
            )
            completion, actions, parallel_tool_calls = await self._complete_valid_action(
                pid,
                messages,
                openai_tools,
                response_scope_fingerprint=response_scope_fingerprint,
            )
            return await self._dispatch_completed_llm_action(
                pid=pid,
                completion=completion,
                actions=actions,
                parallel_tool_calls=parallel_tool_calls,
            )
        except _LLMReleaseApprovalRequired as exc:
            return self._wait_for_llm_release(pid, exc)
        except HumanApprovalRequired as exc:
            self.runtime.audit.record(
                actor=pid,
                action="llm.action_waiting_human",
                target=f"human_request:{exc.request_id}",
                decision={"request_id": exc.request_id, "message": str(exc)},
            )
            return {"ok": False, "waiting_human": True, "request_id": exc.request_id}
        except ProcessWaitRequired as exc:
            self.runtime.audit.record(
                actor=pid,
                action="llm.action_waiting_child",
                target=f"process:{exc.child_pid}",
                decision={"child_pid": exc.child_pid, "message": str(exc)},
            )
            return {"ok": False, "waiting_event": True, "child_pid": exc.child_pid}
        except ProcessMessageWaitRequired as exc:
            self.runtime.audit.record(
                actor=pid,
                action="llm.action_waiting_message",
                target=f"process:{pid}",
                decision={"recipient_pid": exc.recipient_pid, "filters": exc.filters, "message": str(exc)},
            )
            return {"ok": False, "waiting_message": True, "filters": exc.filters}
        except ResourceLimitExceeded as exc:
            self.runtime.resources.kill_if_exceeded(pid, reason=str(exc))
            self.runtime.audit.record(
                actor=pid,
                action="llm.resource_limit_exceeded",
                target=f"process:{pid}",
                decision={"error": str(exc)},
            )
            return {"ok": False, "resource_limit_exceeded": True, "error": str(exc)}
        except Exception as exc:
            self.runtime.process.exit(pid, failed=True, message=f"LLM quantum failed: {exc}")
            self.runtime.audit.record(
                actor=pid,
                action="llm.action_failed",
                target=f"process:{pid}",
                decision={"error": str(exc)},
            )
            return {"ok": False, "error": str(exc)}
        finally:
            self.runtime.data_flow.reset(flow_token)

    def _completed_action_result(
        self,
        pid: str,
        action: dict[str, Any],
        result: dict[str, Any],
        content_preview: str,
        tool_call_count: int,
        resumed_after_human: bool = False,
        resumed_after_message: bool = False,
    ) -> dict[str, Any]:
        self.runtime.audit.record(
            actor=pid,
            action="llm.action",
            target=action.get("action"),
            decision={
                "action": sanitize_for_observability(action),
                "result": sanitize_for_observability(result),
                "content_preview": content_preview,
                "tool_call_count": tool_call_count,
                "resumed_after_human": resumed_after_human,
                "resumed_after_message": resumed_after_message,
            },
        )
        payload = {"ok": True, "action": action, "result": result}
        if resumed_after_human:
            payload["resumed_after_human"] = True
        if resumed_after_message:
            payload["resumed_after_message"] = True
        return payload

    async def _dispatch_completed_llm_action(
        self,
        *,
        pid: str,
        completion: Any,
        actions: list[dict[str, Any]],
        parallel_tool_calls: bool,
        resumed_after_human: bool = False,
    ) -> dict[str, Any]:
        if parallel_tool_calls and len(actions) > 1:
            return await self._dispatch_action_batch(
                pid=pid,
                completion=completion,
                actions=actions,
            )
        action = actions[-1]
        tool_call_context = self._selected_completion_tool_call_context(completion)
        content_preview = str(completion.content)[: self.config.llm.content_preview_chars]
        tool_call_count = len(completion.tool_calls)
        try:
            result = await self.adispatch(pid, action)
        except HumanApprovalRequired as exc:
            return self._wait_for_human_action(
                pid=pid,
                action=action,
                request_id=exc.request_id,
                message=str(exc),
                content_preview=content_preview,
                tool_call_count=tool_call_count,
                **tool_call_context,
            )
        except ProcessWaitRequired as exc:
            return self._wait_for_child_action(
                pid=pid,
                action=exc.resume_action or action,
                child_pid=exc.child_pid,
                message=str(exc),
                content_preview=content_preview,
                tool_call_count=tool_call_count,
                **tool_call_context,
            )
        except ProcessMessageWaitRequired as exc:
            return self._wait_for_message_action(
                pid=pid,
                action=action,
                filters=exc.filters,
                message=str(exc),
                content_preview=content_preview,
                tool_call_count=tool_call_count,
                **tool_call_context,
            )
        self._persist_response_tool_output(
            pid=pid,
            result=result,
            **tool_call_context,
        )
        return self._completed_action_result(
            pid=pid,
            action=action,
            result=result,
            content_preview=content_preview,
            tool_call_count=tool_call_count,
            resumed_after_human=resumed_after_human,
        )

    async def _dispatch_action_batch(
        self,
        *,
        pid: str,
        completion: Any,
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        completed_actions: list[dict[str, Any]] = []
        completed_results: list[dict[str, Any]] = []
        content_preview = str(getattr(completion, "content", ""))[: self.config.llm.content_preview_chars]
        tool_call_count = len(getattr(completion, "tool_calls", []) or [])
        stop_reason = "completed"
        stopped_action: dict[str, Any] | None = None
        stopped_result: dict[str, Any] | None = None

        for action_index, action in enumerate(actions):
            tool_call_context = self._completion_tool_call_context(completion, index=action_index)
            try:
                result = await self.adispatch(pid, action)
            except HumanApprovalRequired as exc:
                stop_reason = "waiting_human"
                payload = self._wait_for_human_action(
                    pid=pid,
                    action=action,
                    request_id=exc.request_id,
                    message=str(exc),
                    content_preview=content_preview,
                    tool_call_count=tool_call_count,
                    **tool_call_context,
                )
                self._record_action_batch(
                    pid=pid,
                    actions=actions,
                    completed_actions=completed_actions,
                    completed_results=completed_results,
                    content_preview=content_preview,
                    tool_call_count=tool_call_count,
                    stop_reason=stop_reason,
                    pending_action=action,
                )
                return self._with_parallel_batch_progress(payload, completed_actions, completed_results)
            except ProcessWaitRequired as exc:
                stop_reason = "waiting_child"
                pending_action = exc.resume_action or action
                payload = self._wait_for_child_action(
                    pid=pid,
                    action=pending_action,
                    child_pid=exc.child_pid,
                    message=str(exc),
                    content_preview=content_preview,
                    tool_call_count=tool_call_count,
                    **tool_call_context,
                )
                self._record_action_batch(
                    pid=pid,
                    actions=actions,
                    completed_actions=completed_actions,
                    completed_results=completed_results,
                    content_preview=content_preview,
                    tool_call_count=tool_call_count,
                    stop_reason=stop_reason,
                    pending_action=pending_action,
                )
                return self._with_parallel_batch_progress(payload, completed_actions, completed_results)
            except ProcessMessageWaitRequired as exc:
                stop_reason = "waiting_message"
                payload = self._wait_for_message_action(
                    pid=pid,
                    action=action,
                    filters=exc.filters,
                    message=str(exc),
                    content_preview=content_preview,
                    tool_call_count=tool_call_count,
                    **tool_call_context,
                )
                self._record_action_batch(
                    pid=pid,
                    actions=actions,
                    completed_actions=completed_actions,
                    completed_results=completed_results,
                    content_preview=content_preview,
                    tool_call_count=tool_call_count,
                    stop_reason=stop_reason,
                    pending_action=action,
                )
                return self._with_parallel_batch_progress(payload, completed_actions, completed_results)
            except ResourceLimitExceeded:
                self._record_action_batch(
                    pid=pid,
                    actions=actions,
                    completed_actions=completed_actions,
                    completed_results=completed_results,
                    content_preview=content_preview,
                    tool_call_count=tool_call_count,
                    stop_reason="resource_limit_exceeded",
                )
                raise

            if result.get("interrupted_by_message"):
                stop_reason = "interrupted_by_message"
                stopped_action = action
                stopped_result = result
                break
            self._persist_response_tool_output(pid=pid, result=result, **tool_call_context)
            completed_actions.append(action)
            completed_results.append(result)
            if not result.get("ok"):
                stop_reason = "tool_failed"
                break
            if result.get("message_notice"):
                stop_reason = "message_notice"
                break
            if self._process_is_terminal(pid):
                stop_reason = "process_terminal"
                break

        self._record_action_batch(
            pid=pid,
            actions=actions,
            completed_actions=completed_actions,
            completed_results=completed_results,
            content_preview=content_preview,
            tool_call_count=tool_call_count,
            stop_reason=stop_reason,
            stopped_action=stopped_action,
            stopped_result=stopped_result,
        )
        payload: dict[str, Any] = {
            "ok": True,
            "parallel_tool_calls": True,
            "actions": completed_actions,
            "results": completed_results,
            "tool_call_count": tool_call_count,
            "executed_count": len(completed_actions),
            "stop_reason": stop_reason,
        }
        if completed_actions:
            payload["action"] = completed_actions[-1]
            payload["result"] = completed_results[-1]
        elif stopped_action is not None and stopped_result is not None:
            payload["action"] = stopped_action
            payload["result"] = stopped_result
        if stopped_action is not None and stopped_result is not None:
            payload.update(
                {
                    "stopped_action": stopped_action,
                    "stopped_result": stopped_result,
                }
            )
        return payload

    def _record_action_batch(
        self,
        *,
        pid: str,
        actions: list[dict[str, Any]],
        completed_actions: list[dict[str, Any]],
        completed_results: list[dict[str, Any]],
        content_preview: str,
        tool_call_count: int,
        stop_reason: str,
        pending_action: dict[str, Any] | None = None,
        stopped_action: dict[str, Any] | None = None,
        stopped_result: dict[str, Any] | None = None,
    ) -> None:
        self.runtime.audit.record(
            actor=pid,
            action="llm.action_batch",
            target=f"process:{pid}",
            decision={
                "actions": sanitize_for_observability(actions),
                "completed_actions": sanitize_for_observability(completed_actions),
                "completed_results": sanitize_for_observability(completed_results),
                "pending_action": sanitize_for_observability(pending_action) if pending_action else None,
                "stopped_action": sanitize_for_observability(stopped_action) if stopped_action else None,
                "stopped_result": sanitize_for_observability(stopped_result) if stopped_result else None,
                "content_preview": content_preview,
                "tool_call_count": tool_call_count,
                "requested_count": len(actions),
                "executed_count": len(completed_actions),
                "stop_reason": stop_reason,
            },
        )

    @staticmethod
    def _with_parallel_batch_progress(
        payload: dict[str, Any],
        completed_actions: list[dict[str, Any]],
        completed_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload["parallel_tool_calls"] = True
        payload["completed_actions"] = completed_actions
        payload["completed_results"] = completed_results
        payload["executed_count"] = len(completed_actions)
        return payload

    def _process_is_terminal(self, pid: str) -> bool:
        return self.runtime.process.get(pid).status in {
            ProcessStatus.EXITED,
            ProcessStatus.FAILED,
            ProcessStatus.KILLED,
        }

    def _wait_for_human_action(
        self,
        pid: str,
        action: dict[str, Any],
        request_id: str,
        message: str,
        content_preview: str,
        tool_call_count: int,
        response_id: str | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        resume_token = self._persist_pending_action(
            pid,
            wait_type="human",
            request_id=request_id,
            action=action,
            content_preview=content_preview,
            tool_call_count=tool_call_count,
            response_id=response_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        operation_context = self.runtime.store.get_llm_pending_action(pid) or {}
        self._pending_human_actions[pid] = {
            "request_id": request_id,
            "resume_token": resume_token,
            "llm_operation_id": operation_context.get("llm_operation_id"),
            "tool_operation_id": operation_context.get("tool_operation_id"),
            "action": dict(action),
            "content_preview": content_preview,
            "tool_call_count": tool_call_count,
            "response_id": response_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
        }
        self.runtime.audit.record(
            actor=pid,
            action="llm.action_waiting_human",
            target=f"human_request:{request_id}",
            decision={
                "request_id": request_id,
                "action": sanitize_for_observability(action),
                "message": message,
                "tool_call_count": tool_call_count,
            },
        )
        return {"ok": False, "waiting_human": True, "request_id": request_id}

    def _wait_for_llm_release(
        self,
        pid: str,
        exc: _LLMReleaseApprovalRequired,
    ) -> dict[str, Any]:
        prepared = dict(exc.prepared_request)
        durable_action = (
            prepared
            if self.config.llm.persist_full_io
            else self._redacted_llm_release_action(prepared)
        )
        resume_token = self._persist_pending_action(
            pid,
            wait_type="llm_release",
            request_id=exc.request_id,
            action=durable_action,
            content_preview="",
            tool_call_count=0,
        )
        operation_context = self.runtime.store.get_llm_pending_action(pid) or {}
        self._pending_llm_release_actions[pid] = {
            "request_id": exc.request_id,
            "resume_token": resume_token,
            "llm_operation_id": operation_context.get("llm_operation_id"),
            "tool_operation_id": operation_context.get("tool_operation_id"),
            "action": prepared,
            "data_flow_context": dict(
                operation_context.get("data_flow_context") or {}
            ),
        }
        self.runtime.audit.record(
            actor=pid,
            action="llm.release_waiting_human",
            target=f"human_request:{exc.request_id}",
            decision={
                "request_id": exc.request_id,
                "profile_id": prepared.get("profile_id"),
                "payload_sha256": dict(prepared.get("canonical_args") or {}).get(
                    "payload_sha256"
                ),
                "attempt": prepared.get("attempt"),
            },
        )
        return {
            "ok": False,
            "waiting_human": True,
            "request_id": exc.request_id,
        }

    @classmethod
    def _redacted_llm_release_action(
        cls,
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        canonical_args = dict(prepared.get("canonical_args") or {})
        return {
            "kind": "llm_release_request_redacted",
            "schema_version": 1,
            "pid": prepared.get("pid"),
            "call_id": prepared.get("call_id"),
            "profile_id": prepared.get("profile_id"),
            "payload_sha256": canonical_args.get("payload_sha256"),
            "prepared_request_sha256": cls._prepared_llm_release_sha256(prepared),
            "attempt": prepared.get("attempt"),
            "payload_retained": False,
        }

    @staticmethod
    def _prepared_llm_release_sha256(prepared: dict[str, Any]) -> str:
        return hashlib.sha256(
            dumps(to_jsonable(prepared)).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _resolve_pending_llm_release_payload(
        cls,
        *,
        in_memory_action: dict[str, Any],
        durable_action: dict[str, Any],
    ) -> dict[str, Any]:
        durable_kind = str(durable_action.get("kind") or "")
        if durable_kind == "llm_release_request":
            return durable_action
        if durable_kind != "llm_release_request_redacted":
            raise RuntimeError("durable pending LLM release has an invalid payload kind")
        if durable_action.get("schema_version") != 1:
            raise RuntimeError(
                "durable pending LLM release has an unsupported redacted schema"
            )

        expected_sha256 = str(
            durable_action.get("prepared_request_sha256") or ""
        )
        if len(expected_sha256) != 64:
            raise RuntimeError(
                "durable pending LLM release is missing its prepared-request hash"
            )
        if str(in_memory_action.get("kind") or "") != "llm_release_request":
            raise _LLMReleasePayloadUnavailable(
                "prepared LLM release payload is unavailable because full-I/O "
                "retention was disabled and the exact in-memory request was lost"
            )
        actual_sha256 = cls._prepared_llm_release_sha256(in_memory_action)
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise RuntimeError(
                "in-memory prepared LLM release does not match its durable hash"
            )
        return in_memory_action

    def _record_llm_release_payload_unavailable(
        self,
        *,
        pid: str,
        request_id: str,
        claimed: dict[str, Any],
        error: RuntimeError,
    ) -> None:
        durable_action = dict(claimed.get("action") or {})
        self.runtime.audit.record(
            actor="llm.executor",
            action="llm.release_resume_payload_unavailable",
            target=f"human_request:{request_id}",
            decision={
                "request_id": request_id,
                "profile_id": durable_action.get("profile_id"),
                "payload_sha256": durable_action.get("payload_sha256"),
                "prepared_request_sha256": durable_action.get(
                    "prepared_request_sha256"
                ),
                "error_type": type(error).__name__,
                "persist_full_io": self.config.llm.persist_full_io,
                "replayed": False,
            },
        )

    def _wait_for_child_action(
        self,
        pid: str,
        action: dict[str, Any],
        child_pid: str,
        message: str,
        content_preview: str,
        tool_call_count: int,
        response_id: str | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        resume_token = self._persist_pending_action(
            pid,
            wait_type="child",
            child_pid=child_pid,
            action=action,
            content_preview=content_preview,
            tool_call_count=tool_call_count,
            response_id=response_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        operation_context = self.runtime.store.get_llm_pending_action(pid) or {}
        self._pending_wait_actions[pid] = {
            "child_pid": child_pid,
            "resume_token": resume_token,
            "llm_operation_id": operation_context.get("llm_operation_id"),
            "tool_operation_id": operation_context.get("tool_operation_id"),
            "action": dict(action),
            "content_preview": content_preview,
            "tool_call_count": tool_call_count,
            "response_id": response_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
        }
        self.runtime.audit.record(
            actor=pid,
            action="llm.action_waiting_child",
            target=f"process:{child_pid}",
            decision={
                "child_pid": child_pid,
                "action": sanitize_for_observability(action),
                "message": message,
                "tool_call_count": tool_call_count,
            },
        )
        return {"ok": False, "waiting_event": True, "child_pid": child_pid}

    def _wait_for_message_action(
        self,
        pid: str,
        action: dict[str, Any],
        filters: dict[str, Any],
        message: str,
        content_preview: str,
        tool_call_count: int,
        response_id: str | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
    ) -> dict[str, Any]:
        resume_token = self._persist_pending_action(
            pid,
            wait_type="message",
            filters=filters,
            action=action,
            content_preview=content_preview,
            tool_call_count=tool_call_count,
            response_id=response_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
        operation_context = self.runtime.store.get_llm_pending_action(pid) or {}
        self._pending_message_actions[pid] = {
            "filters": dict(filters),
            "resume_token": resume_token,
            "llm_operation_id": operation_context.get("llm_operation_id"),
            "tool_operation_id": operation_context.get("tool_operation_id"),
            "action": dict(action),
            "content_preview": content_preview,
            "tool_call_count": tool_call_count,
            "response_id": response_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
        }
        self.runtime.audit.record(
            actor=pid,
            action="llm.action_waiting_message",
            target=f"process:{pid}",
            decision={
                "filters": filters,
                "action": sanitize_for_observability(action),
                "message": message,
                "tool_call_count": tool_call_count,
            },
        )
        return {"ok": False, "waiting_message": True, "filters": filters}

    async def _resume_pending_human_action(self, pid: str) -> dict[str, Any]:
        pending = self._pending_human_actions[pid]
        resume_token = self._pending_resume_token(pending)
        request_id = str(pending["request_id"])
        request = self.runtime.human.get(request_id)
        if request.status == HumanRequestStatus.PENDING:
            return {"ok": False, "waiting_human": True, "request_id": request_id}

        claimed = self.runtime.store.claim_llm_pending_action(pid, resume_token=resume_token)
        if claimed is None:
            self._forget_pending_generation(self._pending_human_actions, pid, resume_token)
            return self._pending_action_resuming_result(pid)
        pending = claimed
        action = dict(pending["action"])
        self._forget_pending_generation(self._pending_human_actions, pid, resume_token)
        if request.status == HumanRequestStatus.APPROVED or (
            self._action_name(action) == "request_permission" and request.status == HumanRequestStatus.REJECTED
        ):
            # Re-dispatch the exact same action. The resume request id is scoped
            # to this single tool call, so concurrent tool calls cannot observe
            # another process' human decision.
            try:
                with self.runtime.data_flow.recovered_source_snapshot_access():
                    result = await self.adispatch(
                        pid,
                        action,
                        context_metadata={
                            **self._pending_data_flow_metadata(pending),
                            "human_resume_request_id": request_id,
                            "operation_id": pending.get("tool_operation_id"),
                        },
                    )
            except HumanApprovalRequired as exc:
                return self._wait_for_human_action(
                    pid=pid,
                    action=action,
                    request_id=exc.request_id,
                    message=str(exc),
                    content_preview=str(pending.get("content_preview", "")),
                    tool_call_count=int(pending.get("tool_call_count", 0)),
                    **self._pending_tool_call_context(pending),
                )
            except ProcessMessageWaitRequired as exc:
                return self._wait_for_message_action(
                    pid=pid,
                    action=action,
                    filters=exc.filters,
                    message=str(exc),
                    content_preview=str(pending.get("content_preview", "")),
                    tool_call_count=int(pending.get("tool_call_count", 0)),
                    **self._pending_tool_call_context(pending),
                )
            except ProcessWaitRequired as exc:
                return self._wait_for_child_action(
                    pid=pid,
                    action=exc.resume_action or action,
                    child_pid=exc.child_pid,
                    message=str(exc),
                    content_preview=str(pending.get("content_preview", "")),
                    tool_call_count=int(pending.get("tool_call_count", 0)),
                    **self._pending_tool_call_context(pending),
                )
            self._persist_response_tool_output(
                pid=pid,
                result=result,
                **self._pending_tool_call_context(pending),
            )
            self._clear_pending_action(pid, self._pending_resume_token(pending))
            return self._completed_action_result(
                pid=pid,
                action=action,
                result=result,
                content_preview=str(pending.get("content_preview", "")),
                tool_call_count=int(pending.get("tool_call_count", 0)),
                resumed_after_human=True,
            )

        error = f"human rejected approval request {request_id}"
        # A rejected per-use approval is surfaced as a failed action result, not
        # as a runtime crash, so the process can explain or choose another path.
        self._emit_pending_action_rejected(pid, action, request_id, error)
        result = {"ok": False, "tool_id": None, "result_oid": None, "payload": None, "error": error}
        self._persist_response_tool_output(
            pid=pid,
            result=result,
            **self._pending_tool_call_context(pending),
        )
        self._clear_pending_action(pid, self._pending_resume_token(pending))
        return self._completed_action_result(
            pid=pid,
            action=action,
            result=result,
            content_preview=str(pending.get("content_preview", "")),
            tool_call_count=int(pending.get("tool_call_count", 0)),
            resumed_after_human=True,
        )

    async def _resume_pending_llm_release_action(self, pid: str) -> dict[str, Any]:
        pending = self._pending_llm_release_actions[pid]
        resume_token = self._pending_resume_token(pending)
        request_id = str(pending["request_id"])
        request = self.runtime.human.get(request_id)
        if request.status == HumanRequestStatus.PENDING:
            durable = self.runtime.store.get_llm_pending_action(pid) or {}
            try:
                self._resolve_pending_llm_release_payload(
                    in_memory_action=dict(pending.get("action") or {}),
                    durable_action=dict(durable.get("action") or {}),
                )
            except RuntimeError as error:
                claimed = self.runtime.store.claim_llm_pending_action(
                    pid,
                    resume_token=resume_token,
                )
                if claimed is None:
                    self._forget_pending_generation(
                        self._pending_llm_release_actions,
                        pid,
                        resume_token,
                    )
                    return self._pending_action_resuming_result(pid)
                self._forget_pending_generation(
                    self._pending_llm_release_actions,
                    pid,
                    resume_token,
                )
                self._record_llm_release_payload_unavailable(
                    pid=pid,
                    request_id=request_id,
                    claimed=claimed,
                    error=error,
                )
                raise
            return {"ok": False, "waiting_human": True, "request_id": request_id}

        claimed = self.runtime.store.claim_llm_pending_action(
            pid,
            resume_token=resume_token,
        )
        if claimed is None:
            self._forget_pending_generation(
                self._pending_llm_release_actions,
                pid,
                resume_token,
            )
            return self._pending_action_resuming_result(pid)
        self._forget_pending_generation(
            self._pending_llm_release_actions,
            pid,
            resume_token,
        )

        if request.status != HumanRequestStatus.APPROVED:
            durable_action = dict(claimed.get("action") or {})
            self.runtime.audit.record(
                actor=pid,
                action="llm.release_rejected",
                target=f"human_request:{request_id}",
                decision={
                    "request_id": request_id,
                    "profile_id": durable_action.get("profile_id"),
                    "payload_sha256": (
                        durable_action.get("payload_sha256")
                        or dict(durable_action.get("canonical_args") or {}).get(
                            "payload_sha256"
                        )
                    ),
                },
            )
            self._clear_pending_action(pid, self._pending_resume_token(claimed))
            # A rejected conditional release is a terminal decision for this
            # exact model request.  Leaving the process runnable would rebuild
            # the same prompt on the next quantum and immediately ask for a
            # replacement release approval.  Pause after persisting the
            # structured rejection so an explicit Host resume is required to
            # start a genuinely new model turn.
            self.runtime.process.pause_for_host_resume(
                pid,
                f"LLM data release rejected: {request_id}",
            )
            return {
                "ok": False,
                "llm_release_rejected": True,
                "request_id": request_id,
            }

        try:
            prepared = self._resolve_pending_llm_release_payload(
                in_memory_action=dict(pending.get("action") or {}),
                durable_action=dict(claimed.get("action") or {}),
            )
        except RuntimeError as error:
            self._record_llm_release_payload_unavailable(
                pid=pid,
                request_id=request_id,
                claimed=claimed,
                error=error,
            )
            raise

        try:
            flow_context = DataFlowContext.from_dict(
                dict(claimed.get("data_flow_context") or {})
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("durable LLM release has invalid data-flow context") from exc
        flow_token = self.runtime.data_flow.push(flow_context)
        try:
            try:
                completion, actions, parallel_tool_calls = await self._complete_valid_action(
                    pid,
                    list(prepared.get("base_messages") or []),
                    list(prepared.get("tools") or []),
                    max_attempts=int(prepared.get("max_attempts") or 0) or None,
                    response_scope_fingerprint=(
                        str(prepared["response_scope_fingerprint"])
                        if prepared.get("response_scope_fingerprint") is not None
                        else None
                    ),
                    _prepared_request=prepared,
                )
            except _LLMReleaseApprovalRequired as exc:
                return self._wait_for_llm_release(pid, exc)
            self._clear_pending_action(pid, self._pending_resume_token(claimed))
            return await self._dispatch_completed_llm_action(
                pid=pid,
                completion=completion,
                actions=actions,
                parallel_tool_calls=parallel_tool_calls,
                resumed_after_human=True,
            )
        finally:
            self.runtime.data_flow.reset(flow_token)

    def _action_name(self, action: dict[str, Any]) -> str:
        return str(action.get("action") or action.get("tool") or action.get("name") or "")

    def _pending_action_resuming_result(self, pid: str) -> dict[str, Any]:
        pending = self.runtime.store.get_llm_pending_action(pid) or {}
        status = pending.get("status")
        return {
            "ok": False,
            "pending_action_resuming": status == "resuming",
            "pending_action_already_completed": status == "completed",
            "pending_action_generation_changed": status == "pending",
            "wait_type": pending.get("wait_type"),
        }

    async def _resume_pending_action_fail_closed(self, pid: str, resume: Any) -> dict[str, Any]:
        """Never leave a claimed, non-replayable action on a runnable process."""

        initial = self.runtime.store.get_llm_pending_action(pid) or {}
        initial_token = str(initial.get("resume_token") or "")
        try:
            return await resume(pid)
        except BaseException as exc:
            current = self.runtime.store.get_llm_pending_action(pid) or {}
            if (
                initial_token
                and str(current.get("resume_token") or "") == initial_token
                and current.get("status") in {"resuming", "completed"}
            ):
                self._fail_interrupted_pending_resume(pid, current, exc)
            raise

    def _fail_interrupted_pending_resume(
        self,
        pid: str,
        pending: dict[str, Any],
        error: BaseException,
    ) -> None:
        message = (
            "durable LLM action resume failed after its non-replayable claim; "
            f"automatic replay is disabled: {type(error).__name__}: {error}"
        )
        terminal_error: str | None = None
        process = self.runtime.store.get_process(pid)
        if process is not None and process.status not in {
            ProcessStatus.EXITED,
            ProcessStatus.FAILED,
            ProcessStatus.KILLED,
        }:
            try:
                self.runtime.process.exit(pid, failed=True, message=message)
            except Exception as exc:
                terminal_error = f"{type(exc).__name__}: {exc}"
                # Process finalization can span multiple subsystems.  If it
                # fails after the claim, persist the minimum fail-closed state
                # so a direct run_once caller cannot spin on a RUNNABLE row.
                try:
                    with self.runtime.store.transaction():
                        current = self.runtime.store.get_process(pid)
                        if current is not None and current.status not in {
                            ProcessStatus.EXITED,
                            ProcessStatus.FAILED,
                            ProcessStatus.KILLED,
                        }:
                            current.status = ProcessStatus.FAILED
                            current.status_message = message
                            current.updated_at = utc_now()
                            self.runtime.store.update_process(current)
                except Exception as fallback_exc:
                    terminal_error = (
                        f"{terminal_error}; fallback={type(fallback_exc).__name__}: {fallback_exc}"
                    )
        try:
            self.runtime.audit.record(
                actor="llm.executor",
                action="llm.pending_action_resume_interrupted",
                target=f"process:{pid}",
                decision={
                    "wait_type": pending.get("wait_type"),
                    "status": pending.get("status"),
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "terminal_error": terminal_error,
                    "replayed": False,
                },
            )
        except Exception:
            # Preserve the original post-claim failure.  The durable resuming
            # row and FAILED process state remain the primary evidence.
            pass

    @staticmethod
    def _pending_resume_token(pending: dict[str, Any]) -> str:
        token = str(pending.get("resume_token") or "").strip()
        if not token:
            raise RuntimeError("durable pending LLM action is missing its resume token")
        return token

    @classmethod
    def _forget_pending_generation(
        cls,
        pending_actions: dict[str, dict[str, Any]],
        pid: str,
        resume_token: str,
    ) -> None:
        current = pending_actions.get(pid)
        if current is not None and cls._pending_resume_token(current) == resume_token:
            pending_actions.pop(pid, None)

    @staticmethod
    def _pending_tool_call_context(pending: dict[str, Any]) -> dict[str, str | None]:
        return {
            "response_id": str(pending["response_id"]) if pending.get("response_id") else None,
            "tool_call_id": str(pending["tool_call_id"]) if pending.get("tool_call_id") else None,
            "tool_name": str(pending["tool_name"]) if pending.get("tool_name") else None,
        }

    @staticmethod
    def _completion_tool_call_context(completion: Any, *, index: int) -> dict[str, str | None]:
        response_id = str(getattr(completion, "response_id", "") or "") or None
        tool_calls = list(getattr(completion, "tool_calls", []) or [])
        if not tool_calls:
            return {"response_id": response_id, "tool_call_id": None, "tool_name": None}
        try:
            tool_call = tool_calls[index]
        except IndexError:
            return {"response_id": response_id, "tool_call_id": None, "tool_name": None}
        if not isinstance(tool_call, dict):
            return {"response_id": response_id, "tool_call_id": None, "tool_name": None}
        call_id = str(tool_call.get("call_id") or "").strip() or None
        tool_name = str(tool_call.get("name") or "").strip() or None
        return {"response_id": response_id, "tool_call_id": call_id, "tool_name": tool_name}

    def _selected_completion_tool_call_context(self, completion: Any) -> dict[str, str | None]:
        tool_calls = list(getattr(completion, "tool_calls", []) or [])
        for index in range(len(tool_calls) - 1, -1, -1):
            tool_call = tool_calls[index]
            if not isinstance(tool_call, dict):
                continue
            try:
                tool_call_to_action(tool_call)
            except Exception:
                continue
            return self._completion_tool_call_context(completion, index=index)
        response_id = str(getattr(completion, "response_id", "") or "") or None
        return {"response_id": response_id, "tool_call_id": None, "tool_name": None}

    def _persist_response_tool_output(
        self,
        *,
        pid: str,
        result: dict[str, Any],
        response_id: str | None,
        tool_call_id: str | None,
        tool_name: str | None,
    ) -> None:
        if not response_id or not tool_call_id or not self.config.llm.persist_full_io:
            return
        call = self.runtime.store.get_latest_llm_call(pid=pid, purpose="action_selection")
        if (
            call is None
            or call.api != "responses"
            or call.response_id != response_id
            or call.request_options.get("openai_provider_chain_eligible") is not True
        ):
            return
        manifest = call.request_options.get("openai_response_tool_calls")
        if not isinstance(manifest, list):
            return
        expected_call_ids = [
            str(item.get("call_id") or "").strip()
            for item in manifest
            if isinstance(item, dict)
        ]
        if (
            len(expected_call_ids) != len(manifest)
            or any(not call_id for call_id in expected_call_ids)
            or len(set(expected_call_ids)) != len(expected_call_ids)
            or tool_call_id not in set(expected_call_ids)
        ):
            return
        self.runtime.store.upsert_llm_tool_output(
            pid=pid,
            response_id=response_id,
            call_id=tool_call_id,
            tool_name=tool_name,
            output=dumps(result),
        )

    async def _resume_pending_wait_action(self, pid: str) -> dict[str, Any]:
        pending = self._pending_wait_actions[pid]
        resume_token = self._pending_resume_token(pending)
        child_pid = str(pending["child_pid"])
        child = self.runtime.process.get(child_pid)
        if child.status not in {ProcessStatus.EXITED, ProcessStatus.FAILED, ProcessStatus.KILLED}:
            return {"ok": False, "waiting_event": True, "child_pid": child_pid}

        claimed = self.runtime.store.claim_llm_pending_action(pid, resume_token=resume_token)
        if claimed is None:
            self._forget_pending_generation(self._pending_wait_actions, pid, resume_token)
            return self._pending_action_resuming_result(pid)
        pending = claimed
        action = dict(pending["action"])
        self._forget_pending_generation(self._pending_wait_actions, pid, resume_token)
        try:
            with self.runtime.data_flow.recovered_source_snapshot_access():
                result = await self.adispatch(
                    pid,
                    action,
                    context_metadata={
                        **self._pending_data_flow_metadata(pending),
                        "pending_child_resume": True,
                        "pending_child_pid": child_pid,
                        "operation_id": pending.get("tool_operation_id"),
                    },
                )
        except ProcessWaitRequired as exc:
            return self._wait_for_child_action(
                pid=pid,
                action=exc.resume_action or action,
                child_pid=exc.child_pid,
                message=str(exc),
                content_preview=str(pending.get("content_preview", "")),
                tool_call_count=int(pending.get("tool_call_count", 0)),
                **self._pending_tool_call_context(pending),
            )
        except HumanApprovalRequired as exc:
            return self._wait_for_human_action(
                pid=pid,
                action=action,
                request_id=exc.request_id,
                message=str(exc),
                content_preview=str(pending.get("content_preview", "")),
                tool_call_count=int(pending.get("tool_call_count", 0)),
                **self._pending_tool_call_context(pending),
            )
        except ProcessMessageWaitRequired as exc:
            return self._wait_for_message_action(
                pid=pid,
                action=action,
                filters=exc.filters,
                message=str(exc),
                content_preview=str(pending.get("content_preview", "")),
                tool_call_count=int(pending.get("tool_call_count", 0)),
                **self._pending_tool_call_context(pending),
            )
        self._persist_response_tool_output(
            pid=pid,
            result=result,
            **self._pending_tool_call_context(pending),
        )
        self._clear_pending_action(pid, self._pending_resume_token(pending))
        return self._completed_action_result(
            pid=pid,
            action=action,
            result=result,
            content_preview=str(pending.get("content_preview", "")),
            tool_call_count=int(pending.get("tool_call_count", 0)),
            resumed_after_human=False,
        )

    async def _resume_pending_message_action(self, pid: str) -> dict[str, Any]:
        pending = self._pending_message_actions[pid]
        resume_token = self._pending_resume_token(pending)
        filters = dict(pending.get("filters") or {})
        messages = self.runtime.messages.unread(
            pid,
            kind=filters.get("kind"),
            sender=filters.get("sender"),
            channel=filters.get("channel"),
            correlation_id=filters.get("correlation_id"),
            reply_to=filters.get("reply_to"),
            message_ids=filters.get("message_ids"),
        )
        if not messages:
            return {"ok": False, "waiting_message": True, "filters": filters}
        claimed = self.runtime.store.claim_llm_pending_action(pid, resume_token=resume_token)
        if claimed is None:
            self._forget_pending_generation(self._pending_message_actions, pid, resume_token)
            return self._pending_action_resuming_result(pid)
        pending = claimed
        action = dict(pending["action"])
        self._forget_pending_generation(self._pending_message_actions, pid, resume_token)
        try:
            with self.runtime.data_flow.recovered_source_snapshot_access():
                result = await self.adispatch(
                    pid,
                    action,
                    context_metadata={
                        **self._pending_data_flow_metadata(pending),
                        "operation_id": pending.get("tool_operation_id"),
                    },
                )
        except ProcessMessageWaitRequired as exc:
            return self._wait_for_message_action(
                pid=pid,
                action=action,
                filters=exc.filters,
                message=str(exc),
                content_preview=str(pending.get("content_preview", "")),
                tool_call_count=int(pending.get("tool_call_count", 0)),
                **self._pending_tool_call_context(pending),
            )
        except ProcessWaitRequired as exc:
            return self._wait_for_child_action(
                pid=pid,
                action=exc.resume_action or action,
                child_pid=exc.child_pid,
                message=str(exc),
                content_preview=str(pending.get("content_preview", "")),
                tool_call_count=int(pending.get("tool_call_count", 0)),
                **self._pending_tool_call_context(pending),
            )
        except HumanApprovalRequired as exc:
            return self._wait_for_human_action(
                pid=pid,
                action=action,
                request_id=exc.request_id,
                message=str(exc),
                content_preview=str(pending.get("content_preview", "")),
                tool_call_count=int(pending.get("tool_call_count", 0)),
                **self._pending_tool_call_context(pending),
            )
        self._persist_response_tool_output(
            pid=pid,
            result=result,
            **self._pending_tool_call_context(pending),
        )
        self._clear_pending_action(pid, self._pending_resume_token(pending))
        completed = self._completed_action_result(
            pid=pid,
            action=action,
            result=result,
            content_preview=str(pending.get("content_preview", "")),
            tool_call_count=int(pending.get("tool_call_count", 0)),
            resumed_after_message=True,
        )
        return completed

    def _emit_pending_action_rejected(self, pid: str, action: dict[str, Any], request_id: str, error: str) -> None:
        tool_name = str(action.get("action"))
        source = f"tool:{tool_name}"
        try:
            handle = self.runtime.tools.resolve(tool_name, pid=pid)
            source = f"tool:{handle.tool_id}"
        except Exception:
            pass
        self.runtime.events.emit(
            EventType.TOOL_FAILED,
            source=source,
            target=pid,
            payload={
                "error": error,
                "tool_name": tool_name,
                "request_id": request_id,
                "policy_decision": "deny",
                "policy_reason": "human_rejected_per_use_approval",
            },
        )
        self.runtime.audit.record(
            actor=pid,
            action="llm.pending_action_rejected",
            target=tool_name,
            decision={"request_id": request_id, "action": sanitize_for_observability(action), "error": error},
        )

    def _completion_to_action(self, content: str, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
        errors: list[str] = []
        for tool_call in reversed(tool_calls):
            try:
                return tool_call_to_action(tool_call)
            except Exception as exc:
                errors.append(str(exc))
        try:
            return parse_json_action(content)
        except Exception as exc:
            detail = f"; invalid tool calls: {errors}" if errors else ""
            raise ValueError(
                f"no valid tool call or fallback JSON action found: {exc}{detail}; "
                f"content preview: {content[: self.config.llm.content_preview_chars]!r}"
            ) from exc

    def _completion_to_actions(
        self,
        content: str,
        tool_calls: list[dict[str, Any]],
        *,
        parallel_tool_calls: bool,
        auto_wait_on_empty_tool_calls: bool,
    ) -> tuple[list[dict[str, Any]], bool]:
        if not parallel_tool_calls:
            if not tool_calls and auto_wait_on_empty_tool_calls:
                try:
                    return [parse_json_action(content)], False
                except Exception:
                    return [self._auto_wait_message_action()], True
            return [self._completion_to_action(content, tool_calls)], False
        if not tool_calls:
            try:
                return [parse_json_action(content)], False
            except Exception:
                if auto_wait_on_empty_tool_calls:
                    return [self._auto_wait_message_action()], True
                raise

        actions: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, tool_call in enumerate(tool_calls, start=1):
            try:
                actions.append(tool_call_to_action(tool_call))
            except Exception as exc:
                errors.append(f"{index}: {exc}")
        if errors:
            raise ValueError(f"invalid parallel tool calls: {errors}")
        if not actions:
            raise ValueError("parallel tool call response did not include any function calls")
        return actions, False

    @staticmethod
    def _auto_wait_message_action() -> dict[str, Any]:
        return {"action": "receive_process_messages"}

    async def _complete_valid_action(
        self,
        pid: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_attempts: int | None = None,
        response_scope_fingerprint: str | None = None,
        _prepared_request: dict[str, Any] | None = None,
    ) -> tuple[Any, list[dict[str, Any]], bool]:
        attempt_messages = list(
            (_prepared_request or {}).get("attempt_messages") or messages
        )
        last_error: Exception | None = None
        selected_max_attempts = max_attempts or self.config.llm.action_repair_attempts
        start_attempt = int((_prepared_request or {}).get("attempt") or 1)
        prepared_request = _prepared_request
        for attempt_number in range(start_attempt, selected_max_attempts + 1):
            try:
                completion, parallel_tool_calls, auto_wait_on_empty_tool_calls, profile_id = await self._complete_action_recorded(
                    pid=pid,
                    messages=attempt_messages,
                    tools=tools,
                    attempt=attempt_number,
                    max_attempts=selected_max_attempts,
                    response_scope_fingerprint=response_scope_fingerprint,
                    _prepared_request=prepared_request,
                )
            except _LLMReleaseApprovalRequired as exc:
                exc.prepared_request["base_messages"] = list(messages)
                exc.prepared_request["attempt_messages"] = list(attempt_messages)
                raise
            prepared_request = None
            try:
                raw_actions, auto_wait_used = self._completion_to_actions(
                    completion.content,
                    completion.tool_calls,
                    parallel_tool_calls=parallel_tool_calls,
                    auto_wait_on_empty_tool_calls=auto_wait_on_empty_tool_calls,
                )
                if auto_wait_used:
                    self.runtime.audit.record(
                        actor=pid,
                        action="llm.empty_tool_calls_auto_wait",
                        target=f"process:{pid}",
                        decision={
                            "attempt": attempt_number,
                            "llm_profile_id": profile_id,
                            "action": self._auto_wait_message_action(),
                            "content_preview": completion.content[: self.config.llm.content_preview_chars],
                            "tool_call_count": len(completion.tool_calls),
                        },
                    )
                actions = [
                    self.runtime.tools.normalize_model_action(pid, action)
                    for action in raw_actions
                ]
                for action in actions:
                    self._validate_dispatchable_action(pid, action)
                if parallel_tool_calls and len(actions) > 1:
                    self._preflight_parallel_tool_batch(pid, actions)
                return completion, actions, parallel_tool_calls
            except ValueError as exc:
                last_error = exc
                self.runtime.audit.record(
                    actor=pid,
                    action="llm.action_repair_requested",
                    target=f"process:{pid}",
                    decision={
                        "attempt": attempt_number,
                        "error": str(exc),
                        "tool_call_count": len(completion.tool_calls),
                        "tool_calls_preview": self._tool_call_previews(completion.tool_calls),
                        "content_preview": completion.content[: self.config.llm.content_preview_chars],
                    },
                )
                if attempt_number >= selected_max_attempts:
                    break
                attempt_messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "The previous model response could not be dispatched: "
                            f"{exc}. Choose "
                            f"{'one or more' if parallel_tool_calls else 'exactly one'} "
                            "available OpenAI tool call by its function name. "
                            f"Available tool names: {self.runtime.tools.model_tool_names(pid)}"
                        ),
                    },
                ]
        assert last_error is not None
        raise last_error

    def _preflight_parallel_tool_batch(self, pid: str, actions: list[dict[str, Any]]) -> None:
        resources = getattr(self.runtime, "resources", None)
        if resources is None:
            return
        try:
            resources.preflight(
                pid,
                ResourceUsage(tool_calls=len(actions)),
                source="llm.parallel_tool_batch",
                context={"action_count": len(actions), "actions": [self._action_name(action) for action in actions]},
            )
        except ResourceLimitExceeded as exc:
            raise ValueError(f"parallel tool call batch exceeds remaining tool-call budget: {exc}") from exc

    def _validate_dispatchable_action(self, pid: str, action: dict[str, Any]) -> None:
        name = str(action.get("action") or "").strip()
        if not name:
            raise ValueError("selected action has an empty tool name")
        process = self.runtime.process.get(pid)
        if name not in process.model_tool_table:
            raise ValueError(f"selected action is not in this process model tool projection: {name}")

    def _tool_call_previews(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        previews: list[dict[str, Any]] = []
        for tool_call in tool_calls:
            raw_args = tool_call.get("arguments")
            if isinstance(raw_args, str):
                raw_bytes = raw_args.encode("utf-8", errors="replace")
                try:
                    observable_args: Any = json.loads(raw_args)
                except ValueError:
                    observable_args = raw_args
            else:
                raw_text = repr(raw_args)
                raw_bytes = raw_text.encode("utf-8", errors="replace")
                observable_args = raw_args
            observation = sanitize_for_observability(
                observable_args,
                preview_chars=self.config.llm.tool_arguments_preview_chars,
            )
            previews.append(
                {
                    "id": tool_call.get("id"),
                    "call_id": tool_call.get("call_id"),
                    "name": tool_call.get("name"),
                    "arguments_type": type(raw_args).__name__,
                    "arguments_preview": observation["preview"],
                    "arguments_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                    "arguments_bytes": len(raw_bytes),
                    "arguments_truncated": observation["truncated"],
                    "arguments_redacted": observation["redacted"],
                }
            )
        return previews

    async def _complete_action_recorded(
        self,
        *,
        pid: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        attempt: int,
        max_attempts: int,
        response_scope_fingerprint: str | None = None,
        _force_stateless: bool = False,
        _chain_scope_retry: int = 0,
        _prepared_request: dict[str, Any] | None = None,
    ) -> tuple[Any, bool, bool, str]:
        process = self.runtime.process.get(pid)
        if _prepared_request is None:
            call_id = new_id("llmcall")
            created_at = utc_now()
            profile_id = (
                process.llm_profile_id
                if process is not None
                else self.config.llm.default_profile_id
            )
            request_options = {
                "attempt": attempt,
                "max_attempts": max_attempts,
                "purpose": "action_selection",
                "llm_profile_id": profile_id,
            }
            request_messages = messages
            flow_context = self.runtime.data_flow.current_context()
        else:
            if (
                _prepared_request.get("kind") != "llm_release_request"
                or int(_prepared_request.get("schema_version") or 0) != 1
                or str(_prepared_request.get("pid") or "") != pid
            ):
                raise RuntimeError("invalid durable prepared LLM release request")
            call_id = str(_prepared_request["call_id"])
            created_at = str(_prepared_request["created_at"])
            profile_id = str(_prepared_request["profile_id"])
            request_options = dict(_prepared_request.get("request_options") or {})
            request_messages = list(_prepared_request.get("request_messages") or [])
            tools = list(_prepared_request.get("tools") or [])
            flow_context = DataFlowContext.from_dict(
                dict(_prepared_request.get("flow_context") or {})
            )
        try:
            if _prepared_request is None:
                profile_snapshot = self.runtime.llms.profile_snapshot(profile_id)
                precheck_sink = DataSink(
                    f"llm:{profile_id}",
                    profile_snapshot.identity_sha256,
                )
                self.runtime.data_flow.precheck_egress_clearance(
                    pid=pid,
                    sink=precheck_sink,
                    context=flow_context,
                    payload={"messages": messages, "tools": tools, "profile_id": profile_id},
                )
                self._preflight_llm_call(pid)
                resolved = self.runtime.llms.resolve(profile_id, snapshot=profile_snapshot)
                client = resolved.client
                sink = DataSink(f"llm:{resolved.profile_id}", resolved.identity_sha256)
                if sink != precheck_sink:
                    raise _LLMProviderChainScopeChanged(
                        "LLM profile Sink changed after egress precheck"
                    )
                data_flow_chain_fingerprint = self._data_flow_provider_chain_fingerprint(
                    pid=pid,
                    sink=sink,
                    context=flow_context,
                )
                source_refs_fingerprint = flow_context.source_refs_hash()
                provider_chain_fingerprint = self._combined_provider_chain_fingerprint(
                    client,
                    data_flow_chain_fingerprint,
                )
                if _force_stateless:
                    previous_response_id, previous_tool_outputs = None, []
                else:
                    previous_response_id, previous_tool_outputs = self._previous_response_state_for_state(
                        pid,
                        resolved.profile_id,
                        client,
                        response_scope_fingerprint=response_scope_fingerprint,
                        provider_chain_fingerprint=provider_chain_fingerprint,
                    )
                request_messages = self._messages_with_tool_outputs(messages, previous_tool_outputs)
                parallel_tool_calls = bool(resolved.parallel_tool_calls)
                auto_wait_on_empty_tool_calls = bool(resolved.auto_wait_on_empty_tool_calls)
                temperature = resolved.temperature
                max_tokens = resolved.max_tokens
                request_options.update(
                    {
                        "llm_profile_id": resolved.profile_id,
                        "client_class": type(client).__name__,
                        "real_llm_client": isinstance(client, LLMClient),
                        "openai_tool_schema": self._tool_schema_observation(tools),
                        "openai_responses_previous_response_id_enabled": bool(
                            isinstance(client, LLMClient) and client.responses_previous_response_id
                        ),
                        "openai_provider_chain_eligible": bool(
                            isinstance(client, LLMClient)
                            and client.responses_previous_response_id
                            and client.store
                            and client._use_responses_api()
                            and client._use_openai_request_options()
                            and provider_chain_fingerprint is not None
                        ),
                        "openai_previous_response_id": previous_response_id,
                        "openai_previous_response_tool_output_count": len(previous_tool_outputs),
                        "openai_response_scope_fingerprint": response_scope_fingerprint,
                        "openai_provider_chain_fingerprint": provider_chain_fingerprint,
                        "data_flow_provider_chain_fingerprint": data_flow_chain_fingerprint,
                        "data_flow_provider_source_refs_sha256": source_refs_fingerprint,
                        "openai_prompt_cache_key_configured": bool(
                            isinstance(client, LLMClient) and client.prompt_cache_key
                        ),
                        "openai_prompt_cache_retention": (
                            client.prompt_cache_retention if isinstance(client, LLMClient) else None
                        ),
                        "openai_safety_identifier_configured": bool(
                            isinstance(client, LLMClient) and client.safety_identifier
                        ),
                        "openai_parallel_tool_calls_enabled": parallel_tool_calls,
                        "agent_libos_auto_wait_on_empty_tool_calls_enabled": auto_wait_on_empty_tool_calls,
                    }
                )
                egress_payload = {
                    "messages": request_messages,
                    "tools": tools,
                    "profile_id": resolved.profile_id,
                    "previous_response_id": previous_response_id,
                    "parallel_tool_calls": parallel_tool_calls,
                }
                canonical_args = {
                    "profile_id": resolved.profile_id,
                    "sink_identity_sha256": sink.identity_sha256,
                    "payload_sha256": hashlib.sha256(
                        dumps(to_jsonable(egress_payload)).encode("utf-8")
                    ).hexdigest(),
                    "attempt": attempt,
                }
            else:
                self._preflight_llm_call(pid)
                resolved = self.runtime.llms.resolve(profile_id)
                client = resolved.client
                sink_data = dict(_prepared_request.get("sink") or {})
                sink = DataSink(
                    identity=str(sink_data["identity"]),
                    identity_sha256=sink_data.get("identity_sha256"),
                    trust_identity=sink_data.get("trust_identity"),
                    trust_identity_sha256=sink_data.get("trust_identity_sha256"),
                )
                current_sink = DataSink(
                    f"llm:{resolved.profile_id}",
                    resolved.identity_sha256,
                )
                if current_sink != sink:
                    raise _LLMProviderChainScopeChanged(
                        "LLM profile Sink changed while data release was pending"
                    )
                data_flow_chain_fingerprint = str(
                    _prepared_request["data_flow_chain_fingerprint"]
                )
                source_refs_fingerprint = str(
                    _prepared_request["source_refs_fingerprint"]
                )
                prepared_provider_chain_fingerprint = _prepared_request.get(
                    "provider_chain_fingerprint"
                )
                provider_chain_fingerprint = (
                    str(prepared_provider_chain_fingerprint)
                    if prepared_provider_chain_fingerprint is not None
                    else None
                )
                previous_response_id = _prepared_request.get("previous_response_id")
                parallel_tool_calls = bool(_prepared_request["parallel_tool_calls"])
                auto_wait_on_empty_tool_calls = bool(
                    _prepared_request["auto_wait_on_empty_tool_calls"]
                )
                temperature = float(_prepared_request["temperature"])
                max_tokens = int(_prepared_request["max_tokens"])
                egress_payload = dict(_prepared_request.get("egress_payload") or {})
                canonical_args = dict(_prepared_request.get("canonical_args") or {})
            invocation = ProtectedOperationInvocation(
                pid=pid,
                actor=pid,
                target=sink.identity,
                canonical_args=canonical_args,
                observation={
                    **canonical_args,
                    "message_count": len(request_messages),
                    "tool_count": len(tools),
                    "source_count": len(flow_context.source_refs),
                },
                data_sink=sink,
                data_flow_context=flow_context,
                data_flow_ingress_context=self.runtime.data_flow.unclassified_ingress_context(
                    flow_context,
                    origin="external:llm",
                ),
                data_flow_payload=egress_payload,
                data_flow_operation="llm.complete",
                data_flow_allow_recovered_source_snapshots=(_prepared_request is not None),
                prepare=lambda: self._assert_llm_provider_chain_scope(
                    pid=pid,
                    profile_id=resolved.profile_id,
                    context=flow_context,
                    expected_sink=sink,
                    expected_data_flow_fingerprint=data_flow_chain_fingerprint,
                    expected_provider_fingerprint=provider_chain_fingerprint,
                    expected_source_refs_fingerprint=source_refs_fingerprint,
                ),
                failure_evidence=lambda error, phase: ProtectedOperationEvidence(
                    event_type=EventType.EXTERNAL_WRITE,
                    event_source=pid,
                    event_target=sink.identity,
                    event_payload={
                        "adapter": "llm",
                        "profile_id": resolved.profile_id,
                        "outcome": "unknown",
                        "phase": phase,
                    },
                    audit_action="primitive.llm.complete.failed",
                    audit_actor=pid,
                    audit_target=sink.identity,
                    audit_decision={
                        **canonical_args,
                        "error_type": type(error).__name__,
                        "phase": phase,
                        "effect_outcome": "unknown",
                    },
                    input_refs=tuple(item.oid for item in flow_context.source_refs),
                ),
            )
            with self.runtime.protected_operations.start(
                "primitive.llm.complete",
                invocation,
                provider=client,
            ) as protected:
                async def dispatch_bound_request() -> Any:
                    # This runs after the SDK marks the provider phase dispatched
                    # but before the client can perform DNS or transport I/O. A
                    # mismatch is certified as not-started by the private
                    # exception and the request is rebuilt without provider state.
                    self._assert_llm_provider_chain_scope(
                        pid=pid,
                        profile_id=resolved.profile_id,
                        context=flow_context,
                        expected_sink=sink,
                        expected_data_flow_fingerprint=data_flow_chain_fingerprint,
                        expected_provider_fingerprint=provider_chain_fingerprint,
                        expected_source_refs_fingerprint=source_refs_fingerprint,
                    )
                    return await self._complete_action(
                        client,
                        request_messages,
                        tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        previous_response_id=previous_response_id,
                        parallel_tool_calls=parallel_tool_calls,
                    )

                completion = await protected.acall(
                    ProviderPhase(
                        "provider_request",
                        state_mutation=True,
                        information_flow=True,
                    ),
                    dispatch_bound_request,
                )
                completion = protected.complete(
                    completion,
                    ProtectedOperationEvidence(
                        event_type=EventType.EXTERNAL_WRITE,
                        event_source=pid,
                        event_target=sink.identity,
                        event_payload={
                            "adapter": "llm",
                            "profile_id": resolved.profile_id,
                            "status": "ok",
                            "request_id": getattr(completion, "request_id", None),
                            "response_id": getattr(completion, "response_id", None),
                        },
                        audit_action="primitive.llm.complete",
                        audit_actor=pid,
                        audit_target=sink.identity,
                        audit_decision={
                            **canonical_args,
                            "status": "ok",
                            "request_id": getattr(completion, "request_id", None),
                            "response_id": getattr(completion, "response_id", None),
                        },
                        input_refs=tuple(item.oid for item in flow_context.source_refs),
                        provider_receipt={
                            "request_id": getattr(completion, "request_id", None),
                            "response_id": getattr(completion, "response_id", None),
                        },
                    ),
                    classification_override=ExternalEffectClassification(
                        rollback_class=ExternalEffectRollbackClass.IRREVERSIBLE,
                        rollback_status=ExternalEffectRollbackStatus.NOT_SUPPORTED,
                        state_mutation=True,
                        information_flow=True,
                        metadata={"outcome": "provider_completed"},
                    ),
                )
        except HumanApprovalRequired as exc:
            prepared_request = dict(_prepared_request or {})
            prepared_request.update(
                {
                    "kind": "llm_release_request",
                    "schema_version": 1,
                    "pid": pid,
                    "call_id": call_id,
                    "created_at": created_at,
                    "profile_id": resolved.profile_id,
                    "request_messages": list(request_messages),
                    "tools": list(tools),
                    "request_options": dict(request_options),
                    "sink": {
                        "identity": sink.identity,
                        "identity_sha256": sink.identity_sha256,
                        "trust_identity": sink.trust_identity,
                        "trust_identity_sha256": sink.trust_identity_sha256,
                    },
                    "flow_context": flow_context.to_dict(),
                    "data_flow_chain_fingerprint": data_flow_chain_fingerprint,
                    "source_refs_fingerprint": source_refs_fingerprint,
                    "provider_chain_fingerprint": provider_chain_fingerprint,
                    "previous_response_id": previous_response_id,
                    "parallel_tool_calls": parallel_tool_calls,
                    "auto_wait_on_empty_tool_calls": auto_wait_on_empty_tool_calls,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "egress_payload": egress_payload,
                    "canonical_args": canonical_args,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "response_scope_fingerprint": response_scope_fingerprint,
                }
            )
            raise _LLMReleaseApprovalRequired(exc, prepared_request) from exc
        except _LLMProviderChainScopeChanged:
            if _chain_scope_retry >= 1:
                raise
            return await self._complete_action_recorded(
                pid=pid,
                messages=messages,
                tools=tools,
                attempt=attempt,
                max_attempts=max_attempts,
                response_scope_fingerprint=response_scope_fingerprint,
                _force_stateless=True,
                _chain_scope_retry=_chain_scope_retry + 1,
            )
        except Exception as exc:
            self._charge_llm_attempt(pid, source="llm.error", context={"error_type": type(exc).__name__})
            self.runtime.store.insert_llm_call(
                LLMCallRecord(
                    call_id=call_id,
                    pid=pid,
                    image_id=process.image_id if process is not None else None,
                    purpose="action_selection",
                    status="error",
                    **observable_llm_call_fields(
                        messages=request_messages,
                        tools=tools,
                        response_content="",
                        tool_calls=[],
                        reasoning=None,
                        raw_response=None,
                        config=self.config,
                    ),
                    request_options=request_options,
                    error=str(exc),
                    created_at=created_at,
                    completed_at=utc_now(),
                )
            )
            self.runtime.operations.link_evidence(
                "llm_call",
                call_id,
                "invocation",
                metadata={"attempt": attempt, "status": "error"},
            )
            raise
        self._charge_llm_attempt(pid, source="llm.completion", context={"usage": dict(getattr(completion, "usage", {}) or {})})
        if getattr(completion, "api", None) == "responses":
            request_options["openai_response_tool_calls"] = self._response_tool_call_manifest(completion)
        observable_fields = observable_llm_call_fields(
            messages=request_messages,
            tools=tools,
            response_content=str(getattr(completion, "content", "")),
            tool_calls=list(getattr(completion, "tool_calls", []) or []),
            reasoning=getattr(completion, "reasoning", None),
            raw_response=getattr(completion, "raw", None),
            config=self.config,
        )
        self.runtime.store.insert_llm_call(
            LLMCallRecord(
                call_id=call_id,
                pid=pid,
                image_id=process.image_id if process is not None else None,
                purpose="action_selection",
                status="ok",
                api=getattr(completion, "api", None),
                model=getattr(completion, "model", None),
                request_id=getattr(completion, "request_id", None),
                response_id=getattr(completion, "response_id", None),
                messages=observable_fields["messages"],
                tools=observable_fields["tools"],
                request_options=request_options,
                response_content=observable_fields["response_content"],
                tool_calls=observable_fields["tool_calls"],
                reasoning=observable_fields["reasoning"],
                usage=dict(getattr(completion, "usage", {}) or {}),
                raw_response=observable_fields["raw_response"],
                observability=observable_fields["observability"],
                created_at=created_at,
                completed_at=utc_now(),
            )
        )
        self.runtime.operations.link_evidence(
            "llm_call",
            call_id,
            "invocation",
            metadata={"attempt": attempt, "status": "ok"},
        )
        if getattr(completion, "request_id", None):
            self.runtime.operations.link_evidence(
                "llm_request",
                str(completion.request_id),
                "invocation",
                metadata={"call_id": call_id},
            )
        self._charge_llm_completion(pid, completion)
        return completion, parallel_tool_calls, auto_wait_on_empty_tool_calls, str(request_options["llm_profile_id"])

    def _preflight_llm_call(self, pid: str) -> None:
        resources = getattr(self.runtime, "resources", None)
        if resources is None:
            return
        resources.preflight(
            pid,
            ResourceUsage(llm_calls=1),
            source="llm.request",
            context={"purpose": "action_selection"},
        )

    def _charge_llm_attempt(self, pid: str, *, source: str, context: dict[str, Any] | None = None) -> None:
        resources = getattr(self.runtime, "resources", None)
        if resources is None:
            return
        resources.charge(
            pid,
            ResourceUsage(llm_calls=1),
            source=source,
            context=context or {},
            allow_overage=True,
            kill_on_exceed=True,
        )

    def _charge_llm_completion(self, pid: str, completion: Any) -> None:
        resources = getattr(self.runtime, "resources", None)
        if resources is None:
            return
        usage = dict(getattr(completion, "usage", {}) or {})
        has_token_limit = resources.has_limit(pid, "max_llm_total_tokens")
        token_keys = {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"}
        if has_token_limit and not any(key in usage for key in token_keys):
            resources.charge(
                pid,
                ResourceUsage(),
                source="llm.completion",
                context={"usage_missing": True},
                allow_overage=False,
                kill_on_exceed=False,
            )
            raise ResourceLimitExceeded("LLM token budget is configured, but provider response did not include token usage")
        if has_token_limit:
            prompt_value = self._budget_usage_int(usage, "prompt_tokens", "input_tokens")
            completion_value = self._budget_usage_int(usage, "completion_tokens", "output_tokens")
            total_value = self._budget_usage_int(usage, "total_tokens")
            prompt_tokens = prompt_value or 0
            completion_tokens = completion_value or 0
            component_total = prompt_tokens + completion_tokens
            total_tokens = component_total if total_value is None else total_value
            if total_value is not None and total_value < component_total:
                raise ResourceLimitExceeded(
                    "LLM token budget is configured, but provider total_tokens is smaller than prompt/completion usage"
                )
        else:
            prompt_tokens = self._usage_int(usage, "prompt_tokens", "input_tokens")
            completion_tokens = self._usage_int(usage, "completion_tokens", "output_tokens")
            total_tokens = self._usage_int(usage, "total_tokens")
            if total_tokens == 0 and (prompt_tokens or completion_tokens):
                total_tokens = prompt_tokens + completion_tokens
        resources.charge(
            pid,
            ResourceUsage(
                llm_prompt_tokens=prompt_tokens,
                llm_completion_tokens=completion_tokens,
                llm_total_tokens=total_tokens,
            ),
            source="llm.completion",
            context={"usage": usage},
            allow_overage=True,
            kill_on_exceed=True,
        )

    def _usage_int(self, usage: dict[str, Any], *keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if value is None:
                continue
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
        return 0

    def _budget_usage_int(self, usage: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            if key not in usage or usage[key] is None:
                continue
            value = usage[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ResourceLimitExceeded(
                    f"LLM token budget is configured, but provider returned invalid {key}: {value!r}"
                )
            return value
        return None

    def _data_flow_provider_chain_fingerprint(
        self,
        *,
        pid: str,
        sink: DataSink,
        context: DataFlowContext,
    ) -> str:
        trust = self.runtime.data_flow.resolve_sink_trust(sink)
        authority_manifest = self.runtime.authority_manifests.get_for_process(pid)
        material = {
            "sink": sink.identity,
            "sink_identity_sha256": sink.identity_sha256,
            "sink_trust_identity": sink.registry_identity,
            "sink_trust_identity_sha256": sink.registry_identity_sha256,
            "registry_generation": self.runtime.store.get_sink_trust_generation(),
            "trust_id": trust.trust_id if trust is not None else None,
            "trust_sha256": trust.spec_hash if trust is not None else None,
            # Provider-side retention is bounded by confidentiality and
            # identity clearance. Trust, integrity, and origin may be lowered
            # by a tool result that is then sent explicitly without changing
            # which data the provider is cleared to retain.
            "clearance_labels_sha256": hashlib.sha256(
                dumps(
                    {
                        "sensitivity": context.labels.sensitivity.value,
                        "tenant": context.labels.tenant,
                        "principal": context.labels.principal,
                    }
                ).encode("utf-8")
            ).hexdigest(),
            "authority_manifest_hash": (
                authority_manifest.manifest_hash
                if authority_manifest is not None
                else None
            ),
        }
        return hashlib.sha256(dumps(material).encode("utf-8")).hexdigest()

    def _combined_provider_chain_fingerprint(
        self,
        client: Any,
        data_flow_fingerprint: str,
    ) -> str | None:
        provider_fingerprint = self._openai_provider_chain_fingerprint(client)
        if provider_fingerprint is None:
            return None
        material = {
            "provider": provider_fingerprint,
            "data_flow": data_flow_fingerprint,
        }
        return hashlib.sha256(dumps(material).encode("utf-8")).hexdigest()

    def _assert_llm_provider_chain_scope(
        self,
        *,
        pid: str,
        profile_id: str,
        context: DataFlowContext,
        expected_sink: DataSink,
        expected_data_flow_fingerprint: str,
        expected_provider_fingerprint: str | None,
        expected_source_refs_fingerprint: str,
    ) -> None:
        current_resolved = self.runtime.llms.resolve(profile_id)
        current_sink = DataSink(
            f"llm:{current_resolved.profile_id}",
            current_resolved.identity_sha256,
        )
        current_data_flow_fingerprint = self._data_flow_provider_chain_fingerprint(
            pid=pid,
            sink=current_sink,
            context=context,
        )
        current_provider_fingerprint = self._combined_provider_chain_fingerprint(
            current_resolved.client,
            current_data_flow_fingerprint,
        )
        if (
            current_sink != expected_sink
            or current_data_flow_fingerprint != expected_data_flow_fingerprint
            or current_provider_fingerprint != expected_provider_fingerprint
            or context.source_refs_hash() != expected_source_refs_fingerprint
        ):
            raise _LLMProviderChainScopeChanged(
                "LLM provider-side response state scope changed before dispatch"
            )

    def _previous_response_state_for_state(
        self,
        pid: str,
        profile_id: str,
        client: Any,
        *,
        response_scope_fingerprint: str | None = None,
        provider_chain_fingerprint: str | None = None,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        if not isinstance(client, LLMClient):
            return None, []
        if (
            not client.responses_previous_response_id
            or not client.store
            or not client._use_responses_api()
            or not client._use_openai_request_options()
        ):
            return None, []
        call = self.runtime.store.get_latest_llm_call(pid=pid, purpose="action_selection")
        if call is None or call.status != "ok" or call.api != "responses" or not call.response_id:
            return None, []
        if call.request_options.get("llm_profile_id") != profile_id:
            return None, []
        if call.request_options.get("openai_response_scope_fingerprint") != response_scope_fingerprint:
            return None, []
        if (
            provider_chain_fingerprint is None
            or call.request_options.get("openai_provider_chain_fingerprint") != provider_chain_fingerprint
        ):
            return None, []

        raw_manifest = call.request_options.get("openai_response_tool_calls")
        if raw_manifest is None:
            # Rows written before durable tool-output tracking are continuable
            # only when the response made no function calls at all.
            if call.tool_calls != []:
                return None, []
            raw_manifest = []
        if not isinstance(raw_manifest, list):
            return None, []

        manifest: list[dict[str, str]] = []
        seen_call_ids: set[str] = set()
        for item in raw_manifest:
            if not isinstance(item, dict):
                return None, []
            call_id = str(item.get("call_id") or "").strip()
            if not call_id or call_id in seen_call_ids:
                return None, []
            seen_call_ids.add(call_id)
            manifest.append(
                {
                    "call_id": call_id,
                    "name": str(item.get("name") or "").strip(),
                }
            )

        output_rows = self.runtime.store.list_llm_tool_outputs(pid=pid, response_id=str(call.response_id))
        outputs_by_call_id = {str(row.get("call_id") or ""): row for row in output_rows}
        if set(outputs_by_call_id) != seen_call_ids:
            return None, []
        tool_messages: list[dict[str, Any]] = []
        for item in manifest:
            output = outputs_by_call_id[item["call_id"]].get("output_text")
            if not isinstance(output, str):
                return None, []
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item["call_id"],
                    "name": item["name"] or None,
                    "content": output,
                }
            )
        return str(call.response_id), tool_messages

    @staticmethod
    def _openai_provider_chain_fingerprint(client: Any) -> str | None:
        """Bind provider-side response state to its actual account boundary.

        The fingerprint is stable across restarts with the same credential but
        does not persist the credential itself.  A model, endpoint, API mode,
        credential/env identity, organization, or project change forces a
        stateless request even when the profile id is reused in place.
        """

        if not isinstance(client, LLMClient):
            return None
        credential = client.api_key or os.getenv(client.api_key_env)
        if not credential:
            return None
        # URL paths are case-sensitive even though scheme/host are not.  Keep
        # the configured spelling so two account gateways that differ only by
        # a case-sensitive path cannot collide and reuse provider-side state.
        # A harmless host-case change may reset the chain, which is safer than
        # treating distinct endpoints as identical.
        base_url = str(client.base_url or "https://api.openai.com/v1").strip().rstrip("/")
        sdk_client = client._async_client or client._client
        organization = (
            getattr(sdk_client, "organization", None)
            or os.getenv("OPENAI_ORGANIZATION")
            or os.getenv("OPENAI_ORG_ID")
        )
        project = getattr(sdk_client, "project", None) or os.getenv("OPENAI_PROJECT")
        material = {
            "client_class": f"{type(client).__module__}.{type(client).__qualname__}",
            "base_url": base_url,
            "model": str(client.model or ""),
            "api_mode": str(client.api_mode or ""),
            "api_key_env": str(client.api_key_env or ""),
            "organization": str(organization or ""),
            "project": str(project or ""),
        }
        return hmac.new(
            str(credential).encode("utf-8"),
            dumps(material).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _messages_with_tool_outputs(
        messages: list[dict[str, Any]],
        tool_outputs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not tool_outputs:
            return messages
        instructions = [message for message in messages if str(message.get("role")) in {"system", "developer"}]
        conversation = [message for message in messages if str(message.get("role")) not in {"system", "developer"}]
        return [*instructions, *tool_outputs, *conversation]

    @staticmethod
    def _response_tool_call_manifest(completion: Any) -> list[dict[str, Any]]:
        manifest: list[dict[str, Any]] = []
        for ordinal, tool_call in enumerate(list(getattr(completion, "tool_calls", []) or [])):
            if not isinstance(tool_call, dict):
                manifest.append({"ordinal": ordinal, "call_id": None, "name": None})
                continue
            manifest.append(
                {
                    "ordinal": ordinal,
                    "call_id": tool_call.get("call_id"),
                    "name": tool_call.get("name"),
                }
            )
        return manifest

    def _responses_state_scope_fingerprint(
        self,
        *,
        pid: str,
        process: Any,
        context: Any,
        tools: list[dict[str, Any]],
    ) -> str:
        context_scope = self._context_scope_for_previous_response(pid)
        material = {
            "pid": pid,
            "image_id": getattr(process, "image_id", None),
            "tool_table": getattr(process, "tool_table", {}),
            "loaded_skills": getattr(process, "loaded_skills", {}),
            "context_scope": context_scope,
            "tools": to_jsonable(tools),
        }
        return hashlib.sha256(dumps(material).encode("utf-8")).hexdigest()

    def _context_scope_for_previous_response(self, pid: str) -> dict[str, Any]:
        return {
            "generation": self.runtime.store.get_llm_context_generation(pid),
        }

    @staticmethod
    def _tool_schema_observation(tools: list[dict[str, Any]]) -> dict[str, int]:
        strict = 0
        non_strict = 0
        for tool in tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            if not isinstance(function, dict):
                continue
            if function.get("strict") is True:
                strict += 1
            else:
                non_strict += 1
        return {"strict": strict, "non_strict": non_strict}

    async def _complete_action(
        self,
        client: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float,
        max_tokens: int,
        previous_response_id: str | None = None,
        parallel_tool_calls: bool,
    ) -> Any:
        kwargs = {"temperature": temperature, "max_tokens": max_tokens}
        if hasattr(client, "acomplete_action"):
            result = (
                client.acomplete_action(
                    messages,
                    tools,
                    **kwargs,
                    previous_response_id=previous_response_id,
                    parallel_tool_calls=parallel_tool_calls,
                )
                if isinstance(client, LLMClient)
                else client.acomplete_action(messages, tools)
            )
            if inspect.isawaitable(result):
                return await result
            return result
        if isinstance(client, LLMClient):
            return await asyncio.to_thread(
                client.complete_action,
                messages,
                tools,
                **kwargs,
                previous_response_id=previous_response_id,
                parallel_tool_calls=parallel_tool_calls,
            )
        return await asyncio.to_thread(client.complete_action, messages, tools)

    def dispatch(
        self,
        pid: str,
        action: dict[str, Any],
        *,
        context_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = str(action["action"])
        args = {key: value for key, value in action.items() if key != "action"}
        if notice := self._pre_tool_interrupt_notice(pid, name):
            return notice
        result = self.runtime.tools.call(pid, name, args, context_metadata=context_metadata)
        if result.result_handle is not None:
            self._add_to_view(pid, result.result_handle)
        post_tool_notice = self._notify_normal_messages(pid)
        return {
            "ok": result.ok,
            "tool_id": result.tool_id,
            "result_oid": result.result_handle.oid if result.result_handle else None,
            "payload": result.payload,
            "error": result.error,
            "message_notice": post_tool_notice,
        }

    async def adispatch(
        self,
        pid: str,
        action: dict[str, Any],
        *,
        context_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = str(action["action"])
        args = {key: value for key, value in action.items() if key != "action"}
        if notice := self._pre_tool_interrupt_notice(pid, name):
            return notice
        result = await self.runtime.tools.acall(pid, name, args, context_metadata=context_metadata)
        if result.result_handle is not None:
            self._add_to_view(pid, result.result_handle)
        post_tool_notice = self._notify_normal_messages(pid)
        return {
            "ok": result.ok,
            "tool_id": result.tool_id,
            "result_oid": result.result_handle.oid if result.result_handle else None,
            "payload": result.payload,
            "error": result.error,
            "message_notice": post_tool_notice,
        }

    def _notify_interrupt_messages(self, pid: str) -> dict[str, Any] | None:
        return self.runtime.messages.notice(
            pid,
            kind=ProcessMessageKind.INTERRUPT,
            phase="before_llm_tool_selection",
            source="llm.executor",
        )

    def _pre_tool_interrupt_notice(self, pid: str, tool_name: str) -> dict[str, Any] | None:
        if tool_name in {"read_process_messages", "receive_process_messages"}:
            return None
        notice = self.runtime.messages.notice(
            pid,
            kind=ProcessMessageKind.INTERRUPT,
            phase="before_tool_call",
            source="llm.executor",
        )
        if notice is None:
            return None
        return {
            "ok": False,
            "tool_id": None,
            "result_oid": None,
            "payload": {"message_notice": notice},
            "error": "unread interrupt process messages are waiting; call read_process_messages or receive_process_messages first",
            "interrupted_by_message": True,
            "message_notice": notice,
        }

    def _notify_normal_messages(self, pid: str) -> dict[str, Any] | None:
        return self.runtime.messages.notice(
            pid,
            kind=ProcessMessageKind.NORMAL,
            phase="after_tool_call",
            source="llm.executor",
        )

    def _handles_for_oids(self, pid: str, oids: list[str]) -> list[ObjectHandle]:
        return [self._handle_for_oid(pid, oid) for oid in oids]

    def _handle_for_oid(self, pid: str, oid: str) -> ObjectHandle:
        process = self.runtime.process.get(pid)
        if process.memory_view is not None:
            for handle in process.memory_view.roots:
                if handle.oid == oid:
                    return handle
        return self.runtime.memory.handle_for_oid(
            pid,
            oid,
            required_rights={ObjectRight.READ.value},
            optional_rights={ObjectRight.MATERIALIZE.value, ObjectRight.LINK.value, ObjectRight.DIFF.value},
            issued_by="llm.executor",
        )

    def _add_to_view(self, pid: str, handle: ObjectHandle) -> None:
        process = self.runtime.process.get(pid)
        if process.memory_view is None:
            process.memory_view = self.runtime.memory.create_view(pid, [handle], mode=ViewMode.READ_ONLY)
        elif all(existing.oid != handle.oid for existing in process.memory_view.roots):
            process.memory_view.roots.append(handle)
        process.updated_at = utc_now()
        self.runtime.store.update_process(process)

    def _persist_pending_action(
        self,
        pid: str,
        *,
        wait_type: str,
        action: dict[str, Any],
        content_preview: str,
        tool_call_count: int,
        request_id: str | None = None,
        child_pid: str | None = None,
        filters: dict[str, Any] | None = None,
        response_id: str | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        resume_token: str | None = None,
    ) -> str:
        # Pending actions are durable process state. They are consumed only after
        # the blocked primitive can be resumed, preserving the original model
        # decision across runtime restarts and human approval latency.
        selected_resume_token = resume_token or new_id("llmwait")
        llm_operation = self.runtime.operations.current()
        tool_operation_id: str | None = None
        if llm_operation is not None:
            evidence_id = request_id or child_pid
            evidence_types = ("human_request",) if request_id else (("process",) if child_pid else ())
            if evidence_id is not None and evidence_types:
                candidates = self.runtime.operations.operation_for_evidence(evidence_types, evidence_id)
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.root_operation_id == llm_operation.root_operation_id
                    and candidate.operation_id != llm_operation.operation_id
                ]
                tool_operation_id = self._select_waiting_tool_operation(candidates)
            if tool_operation_id is None:
                waiting = [
                    candidate
                    for candidate in self.runtime.store.list_operations(
                        root_operation_id=llm_operation.root_operation_id,
                        state="waiting",
                    )
                    if candidate.operation_id != llm_operation.operation_id
                ]
                tool_operation_id = self._select_waiting_tool_operation(waiting)
        self.runtime.store.upsert_llm_pending_action(
            pid,
            {
                "resume_token": selected_resume_token,
                "llm_operation_id": llm_operation.operation_id if llm_operation is not None else None,
                "tool_operation_id": tool_operation_id,
                "wait_type": wait_type,
                "request_id": request_id,
                "child_pid": child_pid,
                "response_id": response_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "filters": dict(filters or {}),
                "action": dict(action),
                "data_flow_context": self._serialize_data_flow_context(
                    self.runtime.data_flow.current_context()
                ),
                "content_preview": content_preview,
                "tool_call_count": tool_call_count,
                "status": "pending",
            },
        )
        return selected_resume_token

    @staticmethod
    def _select_waiting_tool_operation(candidates: list[Any]) -> str | None:
        tool_operations = [
            candidate
            for candidate in candidates
            if candidate.kind.value == "tool_call" and candidate.state.value == "waiting"
        ]
        if len(tool_operations) == 1:
            return tool_operations[0].operation_id
        if len(candidates) == 1:
            return candidates[0].operation_id
        return None

    @staticmethod
    def _serialize_data_flow_context(context: DataFlowContext) -> dict[str, Any]:
        return {
            "labels": context.labels.to_dict(),
            "source_refs": [item.to_dict() for item in context.source_refs],
            "materialization_id": context.materialization_id,
        }

    @staticmethod
    def _pending_data_flow_metadata(pending: dict[str, Any]) -> dict[str, Any]:
        return {"data_flow_context": dict(pending.get("data_flow_context") or {})}

    def _clear_pending_action(self, pid: str, resume_token: str) -> None:
        if not self.runtime.store.complete_llm_pending_action(pid, resume_token=resume_token):
            raise RuntimeError(f"pending LLM action was not claimed before completion: {pid}")

    def _synchronize_pending_action(self, pid: str) -> dict[str, Any] | None:
        pending = self.runtime.store.get_llm_pending_action(pid)
        if pending is None or pending.get("status") != "pending":
            self._clear_in_memory_pending_action(pid)
            return pending
        resume_token = self._pending_resume_token(pending)
        wait_type = str(pending.get("wait_type") or "")
        selected = {
            "llm_release": self._pending_llm_release_actions,
            "human": self._pending_human_actions,
            "child": self._pending_wait_actions,
            "message": self._pending_message_actions,
        }.get(wait_type)
        current = selected.get(pid) if selected is not None else None
        other_present = any(
            pid in mapping
            for mapping in (
                self._pending_llm_release_actions,
                self._pending_human_actions,
                self._pending_wait_actions,
                self._pending_message_actions,
            )
            if mapping is not selected
        )
        if (
            selected is not None
            and current is not None
            and self._pending_resume_token(current) == resume_token
            and not other_present
        ):
            return pending
        self._clear_in_memory_pending_action(pid)
        self._hydrate_pending_action(pending)
        return pending

    def _clear_in_memory_pending_action(self, pid: str) -> None:
        self._pending_llm_release_actions.pop(pid, None)
        self._pending_human_actions.pop(pid, None)
        self._pending_wait_actions.pop(pid, None)
        self._pending_message_actions.pop(pid, None)

    def _hydrate_pending_action(self, pending: dict[str, Any]) -> None:
        pid = str(pending["pid"])
        wait_type = str(pending["wait_type"])
        common = {
            "resume_token": self._pending_resume_token(pending),
            "llm_operation_id": pending.get("llm_operation_id"),
            "tool_operation_id": pending.get("tool_operation_id"),
            "action": dict(pending.get("action") or {}),
            "content_preview": str(pending.get("content_preview") or ""),
            "tool_call_count": int(pending.get("tool_call_count") or 0),
            "response_id": str(pending["response_id"]) if pending.get("response_id") else None,
            "tool_call_id": str(pending["tool_call_id"]) if pending.get("tool_call_id") else None,
            "tool_name": str(pending["tool_name"]) if pending.get("tool_name") else None,
            "data_flow_context": dict(pending.get("data_flow_context") or {}),
        }
        if wait_type == "llm_release" and pending.get("request_id"):
            self._pending_llm_release_actions[pid] = {
                **common,
                "request_id": str(pending["request_id"]),
            }
            return
        if wait_type == "human" and pending.get("request_id"):
            self._pending_human_actions[pid] = {**common, "request_id": str(pending["request_id"])}
            return
        if wait_type == "child" and pending.get("child_pid"):
            restored = {**common, "child_pid": str(pending["child_pid"])}
            self._pending_wait_actions[pid] = restored
            self._restore_pending_compaction_child_goal({**pending, **restored})
            return
        if wait_type == "message":
            self._pending_message_actions[pid] = {
                **common,
                "filters": dict(pending.get("filters") or {}),
            }
            return
        raise RuntimeError(f"invalid durable pending LLM action for {pid}: wait_type={wait_type!r}")

    def _load_pending_actions(self) -> None:
        for pending in self.runtime.store.list_llm_pending_actions(status="resuming"):
            pid = str(pending["pid"])
            process = self.runtime.store.get_process(pid)
            if process is not None and process.status not in {
                ProcessStatus.EXITED,
                ProcessStatus.FAILED,
                ProcessStatus.KILLED,
            }:
                self.runtime.process.exit(
                    pid,
                    failed=True,
                    message="interrupted while resuming a durable LLM action; automatic replay is disabled",
                )
            self.runtime.audit.record(
                actor="llm.executor",
                action="llm.pending_action_resume_interrupted",
                target=f"process:{pid}",
                decision={
                    "wait_type": pending.get("wait_type"),
                    "status": "resuming",
                    "replayed": False,
                },
            )
        for pending in self.runtime.store.list_llm_pending_actions(status="pending"):
            action = dict(pending.get("action") or {})
            if (
                pending.get("wait_type") == "llm_release"
                and action.get("kind") == "llm_release_request_redacted"
            ):
                pid = str(pending["pid"])
                request_id = str(pending.get("request_id") or "")
                try:
                    request = self.runtime.human.get(request_id)
                except NotFound:
                    request = None
                if request is not None and request.status not in {
                    HumanRequestStatus.PENDING,
                    HumanRequestStatus.APPROVED,
                }:
                    # Rejection/cancellation needs no provider payload and can
                    # preserve the ordinary durable rejection-resume path.
                    self._hydrate_pending_action(pending)
                    continue
                resume_token = self._pending_resume_token(pending)
                claimed = self.runtime.store.claim_llm_pending_action(
                    pid,
                    resume_token=resume_token,
                )
                if claimed is None:
                    continue
                error = _LLMReleasePayloadUnavailable(
                    "prepared LLM release payload is unavailable because full-I/O "
                    "retention was disabled and the exact in-memory request was lost"
                )
                self._record_llm_release_payload_unavailable(
                    pid=pid,
                    request_id=request_id,
                    claimed=claimed,
                    error=error,
                )
                self._fail_interrupted_pending_resume(pid, claimed, error)
                continue
            self._hydrate_pending_action(pending)

    def _restore_pending_compaction_child_goal(self, pending: dict[str, Any]) -> None:
        try:
            from agent_libos.tools.builtin.context import restore_pending_compaction_child_goal

            restore_pending_compaction_child_goal(self.runtime, pending)
        except Exception as exc:
            self.runtime.audit.record(
                actor="llm.executor",
                action="llm.pending_compaction_child_restore_failed",
                target=f"process:{pending.get('pid')}",
                decision={"error": str(exc), "child_pid": pending.get("child_pid")},
            )

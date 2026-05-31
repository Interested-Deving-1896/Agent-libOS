from __future__ import annotations

import asyncio
import inspect
from typing import Any, TYPE_CHECKING

from agent_libos.exceptions import HumanApprovalRequired, ProcessWaitRequired
from agent_libos.ids import utc_now
from agent_libos.llm.action_parser import parse_json_action
from agent_libos.llm.client import LLMClient
from agent_libos.llm.context_memory import LLMContextMemory
from agent_libos.llm.prompt import build_system_prompt, build_user_prompt
from agent_libos.llm.tool_protocol import tool_call_to_action
from agent_libos.models import (
    EventType,
    HumanRequestStatus,
    ObjectHandle,
    ProcessStatus,
    ViewMode,
)

if TYPE_CHECKING:
    from agent_libos.runtime.runtime import Runtime


class LLMProcessExecutor:
    """Runs one model-selected tool action per process quantum."""

    def __init__(self, runtime: "Runtime", client: LLMClient | None = None):
        self.runtime = runtime
        self.client = client or LLMClient.from_env()
        # Pending actions are held outside Object Memory because the process has
        # not received a tool result yet. The action is retried after the human
        # queue records a decision, without asking the model for a new action.
        self._pending_human_actions: dict[str, dict[str, Any]] = {}
        self._pending_wait_actions: dict[str, dict[str, Any]] = {}
        self.context_memory = LLMContextMemory(runtime)

    def run_once(self, pid: str) -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.arun_once(pid))
        raise RuntimeError("Cannot call run_once() inside a running event loop. Use await arun_once(...).")

    async def arun_once(self, pid: str) -> dict[str, Any]:
        process = self.runtime.process.get(pid)
        if process.status not in {ProcessStatus.RUNNING, ProcessStatus.RUNNABLE}:
            return {"ok": False, "skipped": True, "status": process.status.value}
        if pid in self._pending_human_actions:
            return await self._resume_pending_human_action(pid)
        if pid in self._pending_wait_actions:
            return await self._resume_pending_wait_action(pid)
        image = self.runtime.images.get(process.image_id) or self.runtime.images["base-agent:v0"]
        if process.memory_view is None:
            process.memory_view = self.runtime.memory.create_view(pid, [], mode=ViewMode.READ_ONLY)
            process.updated_at = utc_now()
            self.runtime.store.update_process(process)

        source_view = self.context_memory.view_without_context(pid, process.memory_view)
        source_context = self.runtime.memory.materialize_context(
            pid,
            source_view,
            policy=image.context_policy,
            budget_tokens=process.resource_budget.max_materialized_tokens,
        )
        events = self.runtime.events.list(target=pid)
        capabilities = self.runtime.capability.capabilities_for(pid)
        # The prompt-visible tool list must match the process tool table. The
        # broker still owns the real execute check, but showing extra tools
        # teaches the model to choose actions the process cannot call.
        tools = self.runtime.tools.visible_tools(pid)
        context = self.context_memory.prepare(
            pid=pid,
            image=image,
            process=process,
            source_context=source_context,
            events=events,
            capabilities=capabilities,
            tools=tools,
        )
        messages = [
            {"role": "system", "content": build_system_prompt(image)},
            {
                "role": "user",
                "content": build_user_prompt(
                    process=process,
                    context=context,
                    events=events,
                    capabilities=capabilities,
                    tools=tools,
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
        try:
            completion = await self._complete_action(messages, self.runtime.tools.openai_tool_schemas(pid))
            action = self._completion_to_action(completion.content, completion.tool_calls)
            try:
                result = await self.adispatch(pid, action)
            except HumanApprovalRequired as exc:
                return self._wait_for_human_action(
                    pid=pid,
                    action=action,
                    request_id=exc.request_id,
                    message=str(exc),
                    content_preview=completion.content[:500],
                    tool_call_count=len(completion.tool_calls),
                )
            except ProcessWaitRequired as exc:
                return self._wait_for_child_action(
                    pid=pid,
                    action=action,
                    child_pid=exc.child_pid,
                    message=str(exc),
                    content_preview=completion.content[:500],
                    tool_call_count=len(completion.tool_calls),
                )
            return self._completed_action_result(
                pid=pid,
                action=action,
                result=result,
                content_preview=completion.content[:500],
                tool_call_count=len(completion.tool_calls),
            )
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
        except Exception as exc:
            self.runtime.process.exit(pid, failed=True, message=f"LLM quantum failed: {exc}")
            self.runtime.audit.record(
                actor=pid,
                action="llm.action_failed",
                target=f"process:{pid}",
                decision={"error": str(exc)},
            )
            return {"ok": False, "error": str(exc)}

    def _completed_action_result(
        self,
        pid: str,
        action: dict[str, Any],
        result: dict[str, Any],
        content_preview: str,
        tool_call_count: int,
        resumed_after_human: bool = False,
    ) -> dict[str, Any]:
        self.runtime.audit.record(
            actor=pid,
            action="llm.action",
            target=action.get("action"),
            decision={
                "action": action,
                "result": result,
                "content_preview": content_preview,
                "tool_call_count": tool_call_count,
                "resumed_after_human": resumed_after_human,
            },
        )
        payload = {"ok": True, "action": action, "result": result}
        if resumed_after_human:
            payload["resumed_after_human"] = True
        return payload

    def _wait_for_human_action(
        self,
        pid: str,
        action: dict[str, Any],
        request_id: str,
        message: str,
        content_preview: str,
        tool_call_count: int,
    ) -> dict[str, Any]:
        self._pending_human_actions[pid] = {
            "request_id": request_id,
            "action": dict(action),
            "content_preview": content_preview,
            "tool_call_count": tool_call_count,
        }
        self.runtime.audit.record(
            actor=pid,
            action="llm.action_waiting_human",
            target=f"human_request:{request_id}",
            decision={
                "request_id": request_id,
                "action": action,
                "message": message,
                "tool_call_count": tool_call_count,
            },
        )
        return {"ok": False, "waiting_human": True, "request_id": request_id}

    def _wait_for_child_action(
        self,
        pid: str,
        action: dict[str, Any],
        child_pid: str,
        message: str,
        content_preview: str,
        tool_call_count: int,
    ) -> dict[str, Any]:
        self._pending_wait_actions[pid] = {
            "child_pid": child_pid,
            "action": dict(action),
            "content_preview": content_preview,
            "tool_call_count": tool_call_count,
        }
        self.runtime.audit.record(
            actor=pid,
            action="llm.action_waiting_child",
            target=f"process:{child_pid}",
            decision={
                "child_pid": child_pid,
                "action": action,
                "message": message,
                "tool_call_count": tool_call_count,
            },
        )
        return {"ok": False, "waiting_event": True, "child_pid": child_pid}

    async def _resume_pending_human_action(self, pid: str) -> dict[str, Any]:
        pending = self._pending_human_actions[pid]
        request_id = str(pending["request_id"])
        request = self.runtime.human.get(request_id)
        if request.status == HumanRequestStatus.PENDING:
            return {"ok": False, "waiting_human": True, "request_id": request_id}

        action = dict(pending["action"])
        self._pending_human_actions.pop(pid, None)
        if request.status == HumanRequestStatus.APPROVED:
            # Re-dispatch the exact same action. This preserves the original
            # model decision and prevents hidden progress before approval.
            try:
                result = await self.adispatch(pid, action)
            except HumanApprovalRequired as exc:
                return self._wait_for_human_action(
                    pid=pid,
                    action=action,
                    request_id=exc.request_id,
                    message=str(exc),
                    content_preview=str(pending.get("content_preview", "")),
                    tool_call_count=int(pending.get("tool_call_count", 0)),
                )
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
        return self._completed_action_result(
            pid=pid,
            action=action,
            result=result,
            content_preview=str(pending.get("content_preview", "")),
            tool_call_count=int(pending.get("tool_call_count", 0)),
            resumed_after_human=True,
        )

    async def _resume_pending_wait_action(self, pid: str) -> dict[str, Any]:
        pending = self._pending_wait_actions[pid]
        child_pid = str(pending["child_pid"])
        child = self.runtime.process.get(child_pid)
        if child.status not in {ProcessStatus.EXITED, ProcessStatus.FAILED, ProcessStatus.KILLED}:
            return {"ok": False, "waiting_event": True, "child_pid": child_pid}

        action = dict(pending["action"])
        self._pending_wait_actions.pop(pid, None)
        try:
            result = await self.adispatch(pid, action)
        except ProcessWaitRequired as exc:
            return self._wait_for_child_action(
                pid=pid,
                action=action,
                child_pid=exc.child_pid,
                message=str(exc),
                content_preview=str(pending.get("content_preview", "")),
                tool_call_count=int(pending.get("tool_call_count", 0)),
            )
        except HumanApprovalRequired as exc:
            return self._wait_for_human_action(
                pid=pid,
                action=action,
                request_id=exc.request_id,
                message=str(exc),
                content_preview=str(pending.get("content_preview", "")),
                tool_call_count=int(pending.get("tool_call_count", 0)),
            )
        return self._completed_action_result(
            pid=pid,
            action=action,
            result=result,
            content_preview=str(pending.get("content_preview", "")),
            tool_call_count=int(pending.get("tool_call_count", 0)),
            resumed_after_human=False,
        )

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
            decision={"request_id": request_id, "action": action, "error": error},
        )

    def _completion_to_action(self, content: str, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
        if tool_calls:
            return tool_call_to_action(tool_calls[-1])
        try:
            return parse_json_action(content)
        except Exception as exc:
            raise ValueError(f"no valid tool call or fallback JSON action found: {exc}; content preview: {content[:500]!r}") from exc

    async def _complete_action(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        if hasattr(self.client, "acomplete_action"):
            result = self.client.acomplete_action(messages, tools)
            if inspect.isawaitable(result):
                return await result
            return result
        return await asyncio.to_thread(self.client.complete_action, messages, tools)

    def dispatch(self, pid: str, action: dict[str, Any]) -> dict[str, Any]:
        name = str(action["action"])
        args = {key: value for key, value in action.items() if key != "action"}
        result = self.runtime.tools.call(pid, name, args)
        if result.result_handle is not None:
            self._add_to_view(pid, result.result_handle)
        return {
            "ok": result.ok,
            "tool_id": result.tool_id,
            "result_oid": result.result_handle.oid if result.result_handle else None,
            "payload": result.payload,
            "error": result.error,
        }

    async def adispatch(self, pid: str, action: dict[str, Any]) -> dict[str, Any]:
        name = str(action["action"])
        args = {key: value for key, value in action.items() if key != "action"}
        result = await self.runtime.tools.acall(pid, name, args)
        if result.result_handle is not None:
            self._add_to_view(pid, result.result_handle)
        return {
            "ok": result.ok,
            "tool_id": result.tool_id,
            "result_oid": result.result_handle.oid if result.result_handle else None,
            "payload": result.payload,
            "error": result.error,
        }

    def _handles_for_oids(self, pid: str, oids: list[str]) -> list[ObjectHandle]:
        return [self._handle_for_oid(pid, oid) for oid in oids]

    def _handle_for_oid(self, pid: str, oid: str) -> ObjectHandle:
        process = self.runtime.process.get(pid)
        if process.memory_view is not None:
            for handle in process.memory_view.roots:
                if handle.oid == oid:
                    return handle
        return self.runtime.capability.handle_for_object(
            pid,
            oid,
            {"read", "materialize", "link", "diff"},
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

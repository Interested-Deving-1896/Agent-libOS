from __future__ import annotations

from typing import Any, TYPE_CHECKING

from agent_libos.exceptions import HumanApprovalRequired
from agent_libos.ids import utc_now
from agent_libos.llm.action_parser import parse_json_action
from agent_libos.llm.client import LLMClient
from agent_libos.llm.prompt import build_system_prompt, build_user_prompt
from agent_libos.llm.tool_protocol import tool_call_to_action
from agent_libos.models import (
    ObjectHandle,
    ProcessStatus,
    ViewMode,
)

if TYPE_CHECKING:
    from agent_libos.runtime.runtime import Runtime


class LLMProcessExecutor:
    def __init__(self, runtime: "Runtime", client: LLMClient | None = None):
        self.runtime = runtime
        self.client = client or LLMClient.from_env()

    def run_once(self, pid: str) -> dict[str, Any]:
        process = self.runtime.process.get(pid)
        if process.status not in {ProcessStatus.RUNNING, ProcessStatus.RUNNABLE}:
            return {"ok": False, "skipped": True, "status": process.status.value}
        image = self.runtime.images.get(process.image_id) or self.runtime.images["base-agent:v0"]
        if process.memory_view is None:
            process.memory_view = self.runtime.memory.create_view(pid, [], mode=ViewMode.READ_ONLY)
            process.updated_at = utc_now()
            self.runtime.store.update_process(process)

        context = self.runtime.memory.materialize_context(
            pid,
            process.memory_view,
            policy=image.context_policy,
            budget_tokens=process.resource_budget.max_materialized_tokens,
        )
        messages = [
            {"role": "system", "content": build_system_prompt(image)},
            {
                "role": "user",
                "content": build_user_prompt(
                    process=process,
                    context=context,
                    events=self.runtime.events.list(target=pid),
                    capabilities=self.runtime.capability.capabilities_for(pid),
                    tools=self.runtime.tools.list(),
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
            completion = self.client.complete_action(messages, tools=self.runtime.tools.openai_tool_schemas())
            action = self._completion_to_action(completion.content, completion.tool_calls)
            result = self.dispatch(pid, action)
            self.runtime.audit.record(
                actor=pid,
                action="llm.action",
                target=action.get("action"),
                decision={
                    "action": action,
                    "result": result,
                    "content_preview": completion.content[:500],
                    "tool_call_count": len(completion.tool_calls),
                },
            )
            return {"ok": True, "action": action, "result": result}
        except HumanApprovalRequired as exc:
            self.runtime.audit.record(
                actor=pid,
                action="llm.action_waiting_human",
                target=f"human_request:{exc.request_id}",
                decision={"request_id": exc.request_id, "message": str(exc)},
            )
            return {"ok": False, "waiting_human": True, "request_id": exc.request_id}
        except Exception as exc:
            self.runtime.process.exit(pid, failed=True, message=f"LLM quantum failed: {exc}")
            self.runtime.audit.record(
                actor=pid,
                action="llm.action_failed",
                target=f"process:{pid}",
                decision={"error": str(exc)},
            )
            return {"ok": False, "error": str(exc)}

    def _completion_to_action(self, content: str, tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
        if tool_calls:
            return tool_call_to_action(tool_calls[-1])
        try:
            return parse_json_action(content)
        except Exception as exc:
            raise ValueError(f"no valid tool call or fallback JSON action found: {exc}; content preview: {content[:500]!r}") from exc

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

from __future__ import annotations

from typing import Any

from agent_libos.exceptions import HumanApprovalRequired
from agent_libos.runtime.runtime import Runtime


def call_tool_with_auto_approval(
    runtime: Runtime,
    pid: str,
    tool: str,
    args: dict[str, Any],
    approval_decision: dict[str, Any] | None = None,
) -> Any:
    try:
        return runtime.tools.call(pid, tool, args)
    except HumanApprovalRequired as exc:
        runtime.human.approve(exc.request_id, approval_decision or {"approved": True, "macro": "auto_approval"})
        return runtime.tools.call(pid, tool, args)


def run_tests_and_summarize(runtime: Runtime, pid: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result = call_tool_with_auto_approval(runtime, pid, tool, args)
    return {"ok": result.ok, "payload": result.payload, "result_oid": result.result_handle.oid if result.result_handle else None}


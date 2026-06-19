from __future__ import annotations

from pydantic import BaseModel

from agent_libos import Runtime
from agent_libos.tools.base import SyncAgentTool, ToolContext


class EmptyArgs(BaseModel):
    pass


class HugeResultTool(SyncAgentTool[EmptyArgs]):
    name = "huge_result"
    description = "Return a result larger than the broker persistence boundary."
    args_schema = EmptyArgs

    def run(self, args: EmptyArgs, ctx: ToolContext) -> dict[str, str]:
        assert ctx.runtime is not None
        return {"blob": "x" * (ctx.runtime.config.tools.tool_result_payload_hard_limit_bytes + 1)}


class TestToolResultLimits:
    def test_tool_result_payload_limit_rejects_before_result_object_creation(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="huge result")
            handle = runtime.tools.register_tool(HugeResultTool(), registered_by="test", ephemeral=True)
            runtime.tools.configure_process_tools(pid, [handle], assigned_by="test")

            result = runtime.tools.call(pid, "huge_result", {})

            assert not result.ok
            assert result.result_handle is None
            assert "tool result payload exceeds" in (result.error or "")
            assert [obj for obj in runtime.store.list_objects() if obj.type.value == "tool_result"] == []
        finally:
            runtime.close()

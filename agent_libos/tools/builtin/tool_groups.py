from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolExecutionError, ToolPolicy


class DiscoverToolGroupsArgs(BaseModel):
    pass


class DiscoverToolGroupsOutput(BaseModel):
    groups: list[dict[str, Any]]


class ActivateToolGroupArgs(BaseModel):
    group: str = Field(description="Tool group name returned by discover_tool_groups.")


class ActivateToolGroupOutput(BaseModel):
    result: dict[str, Any]


class DiscoverToolGroupsTool(SyncAgentTool[DiscoverToolGroupsArgs]):
    name = "discover_tool_groups"
    description = "List image-authorized tool groups without exposing every tool schema up front."
    args_schema = DiscoverToolGroupsArgs
    output_schema = DiscoverToolGroupsOutput
    policy = ToolPolicy(side_effects=False, idempotent=True, timeout_s=DEFAULT_CONFIG.tools.standard_timeout_s)
    tags = ["tool", "discovery", "projection"]

    def run(self, args: DiscoverToolGroupsArgs, ctx: ToolContext) -> DiscoverToolGroupsOutput:
        return DiscoverToolGroupsOutput(groups=_runtime(ctx).tools.tool_groups(ctx.pid))


class ActivateToolGroupTool(SyncAgentTool[ActivateToolGroupArgs]):
    name = "activate_tool_group"
    description = "Expose one image-authorized tool group to this process; this does not grant resource authority."
    args_schema = ActivateToolGroupArgs
    output_schema = ActivateToolGroupOutput
    policy = ToolPolicy(side_effects=True, idempotent=True, timeout_s=DEFAULT_CONFIG.tools.standard_timeout_s)
    tags = ["tool", "activation", "projection"]

    def run(self, args: ActivateToolGroupArgs, ctx: ToolContext) -> ActivateToolGroupOutput:
        return ActivateToolGroupOutput(result=_runtime(ctx).tools.activate_tool_group(ctx.pid, args.group))


def _runtime(ctx: ToolContext) -> Any:
    if ctx.runtime is None:
        raise ToolExecutionError("Runtime is unavailable.")
    return ctx.runtime

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolExecutionError, ToolPolicy

_TOOL_DEFAULTS = DEFAULT_CONFIG.tools


class DiscoverSkillsArgs(BaseModel):
    text: str | None = Field(default=None, description="Optional search text.")
    limit: int | None = Field(default=None, description="Maximum number of skills to return.")


class DiscoverSkillsOutput(BaseModel):
    skills: list[dict[str, Any]]


class ActivateSkillArgs(BaseModel):
    skill_id: str = Field(description="Standard Skill name to activate.")


class ActivateSkillOutput(BaseModel):
    result: dict[str, Any]


class ReadSkillResourceArgs(BaseModel):
    skill_id: str = Field(description="Loaded Skill name.")
    path: str = Field(description="Skill resource path, such as references/foo.md or scripts/tool.ts.")
    max_bytes: int | None = Field(default=None, description="Optional resource byte limit.")


class ReadSkillResourceOutput(BaseModel):
    resource: dict[str, Any]


class UnloadSkillArgs(BaseModel):
    skill_id: str


class UnloadSkillOutput(BaseModel):
    result: dict[str, Any]


class DiscoverSkillsTool(SyncAgentTool[DiscoverSkillsArgs]):
    name = "discover_skills"
    description = "Discover registered standard Agent Skills visible to this process."
    args_schema = DiscoverSkillsArgs
    output_schema = DiscoverSkillsOutput
    policy = ToolPolicy(
        side_effects=False,
        idempotent=True,
        declared_permissions={"skill.read"},
        timeout_s=_TOOL_DEFAULTS.standard_timeout_s,
    )
    tags = ["skill", "inspect"]

    def run(self, args: DiscoverSkillsArgs, ctx: ToolContext) -> DiscoverSkillsOutput:
        return DiscoverSkillsOutput(
            skills=_runtime(ctx).skills.discover_skills(args.text, actor=ctx.pid, limit=args.limit)
        )


class ActivateSkillTool(SyncAgentTool[ActivateSkillArgs]):
    name = "activate_skill"
    description = "Activate a registered standard Agent Skill in this process."
    args_schema = ActivateSkillArgs
    output_schema = ActivateSkillOutput
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        declared_permissions={"skill.read", "tool.write", "tool.table"},
        timeout_s=_TOOL_DEFAULTS.standard_timeout_s,
    )
    tags = ["skill", "activate"]

    def run(self, args: ActivateSkillArgs, ctx: ToolContext) -> ActivateSkillOutput:
        return ActivateSkillOutput(result=_runtime(ctx).skills.activate_skill(ctx.pid, args.skill_id, actor=ctx.pid))


class ReadSkillResourceTool(SyncAgentTool[ReadSkillResourceArgs]):
    name = "read_skill_resource"
    description = "Read a bundled resource from a loaded standard Agent Skill snapshot."
    args_schema = ReadSkillResourceArgs
    output_schema = ReadSkillResourceOutput
    policy = ToolPolicy(
        side_effects=False,
        idempotent=True,
        declared_permissions={"skill.read"},
        timeout_s=_TOOL_DEFAULTS.standard_timeout_s,
    )
    tags = ["skill", "resource", "inspect"]

    def run(self, args: ReadSkillResourceArgs, ctx: ToolContext) -> ReadSkillResourceOutput:
        return ReadSkillResourceOutput(
            resource=_runtime(ctx).skills.read_skill_resource(
                ctx.pid,
                args.skill_id,
                args.path,
                actor=ctx.pid,
                max_bytes=args.max_bytes,
            )
        )


class UnloadSkillTool(SyncAgentTool[UnloadSkillArgs]):
    name = "unload_skill"
    description = "Unload a skill from this process tool table and prompt context."
    args_schema = UnloadSkillArgs
    output_schema = UnloadSkillOutput
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        declared_permissions={"tool.table"},
        timeout_s=_TOOL_DEFAULTS.standard_timeout_s,
    )
    tags = ["skill", "unload"]

    def run(self, args: UnloadSkillArgs, ctx: ToolContext) -> UnloadSkillOutput:
        return UnloadSkillOutput(result=_runtime(ctx).skills.unload_skill(ctx.pid, args.skill_id, actor=ctx.pid))


def _runtime(ctx: ToolContext) -> Any:
    if ctx.runtime is None:
        raise ToolExecutionError("Runtime is unavailable.")
    return ctx.runtime

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


class InspectSkillArgs(BaseModel):
    skill_id: str


class InspectSkillOutput(BaseModel):
    skill: dict[str, Any]


class LoadSkillArgs(BaseModel):
    skill_id: str


class LoadSkillFromYamlArgs(BaseModel):
    path: str = Field(description="Workspace-relative Skill YAML or JSON manifest path.")
    replace: bool = Field(default=False, description="Replace an existing registered skill with the same id.")


class LoadSkillOutput(BaseModel):
    result: dict[str, Any]


class UnloadSkillArgs(BaseModel):
    skill_id: str


class UnloadSkillOutput(BaseModel):
    result: dict[str, Any]


class DiscoverSkillsTool(SyncAgentTool[DiscoverSkillsArgs]):
    name = "discover_skills"
    description = "Discover registered skills visible to this process."
    args_schema = DiscoverSkillsArgs
    output_schema = DiscoverSkillsOutput
    policy = ToolPolicy(side_effects=False, idempotent=True, timeout_s=_TOOL_DEFAULTS.standard_timeout_s)
    tags = ["skill", "inspect"]

    def run(self, args: DiscoverSkillsArgs, ctx: ToolContext) -> DiscoverSkillsOutput:
        return DiscoverSkillsOutput(
            skills=_runtime(ctx).skills.discover_skills(args.text, actor=ctx.pid, limit=args.limit)
        )


class InspectSkillTool(SyncAgentTool[InspectSkillArgs]):
    name = "inspect_skill"
    description = "Inspect a registered skill manifest and metadata."
    args_schema = InspectSkillArgs
    output_schema = InspectSkillOutput
    policy = ToolPolicy(side_effects=False, idempotent=True, timeout_s=_TOOL_DEFAULTS.standard_timeout_s)
    tags = ["skill", "inspect"]

    def run(self, args: InspectSkillArgs, ctx: ToolContext) -> InspectSkillOutput:
        return InspectSkillOutput(skill=_runtime(ctx).skills.inspect_skill(args.skill_id, actor=ctx.pid))


class LoadSkillTool(SyncAgentTool[LoadSkillArgs]):
    name = "load_skill"
    description = "Load a registered skill into this process tool table and prompt context."
    args_schema = LoadSkillArgs
    output_schema = LoadSkillOutput
    policy = ToolPolicy(side_effects=True, idempotent=False, timeout_s=_TOOL_DEFAULTS.standard_timeout_s)
    tags = ["skill", "load"]

    def run(self, args: LoadSkillArgs, ctx: ToolContext) -> LoadSkillOutput:
        return LoadSkillOutput(result=_runtime(ctx).skills.load_skill(ctx.pid, args.skill_id, actor=ctx.pid))


class LoadSkillFromYamlTool(SyncAgentTool[LoadSkillFromYamlArgs]):
    name = "load_skill_from_yaml"
    description = "Read a workspace Skill manifest through the filesystem primitive, register it, and load it."
    args_schema = LoadSkillFromYamlArgs
    output_schema = LoadSkillOutput
    policy = ToolPolicy(side_effects=True, idempotent=False, timeout_s=_TOOL_DEFAULTS.standard_timeout_s)
    tags = ["skill", "load", "yaml"]

    def run(self, args: LoadSkillFromYamlArgs, ctx: ToolContext) -> LoadSkillOutput:
        return LoadSkillOutput(
            result=_runtime(ctx).skills.load_skill_from_workspace_yaml(ctx.pid, args.path, replace=args.replace)
        )


class UnloadSkillTool(SyncAgentTool[UnloadSkillArgs]):
    name = "unload_skill"
    description = "Unload a skill from this process tool table and prompt context."
    args_schema = UnloadSkillArgs
    output_schema = UnloadSkillOutput
    policy = ToolPolicy(side_effects=True, idempotent=False, timeout_s=_TOOL_DEFAULTS.standard_timeout_s)
    tags = ["skill", "unload"]

    def run(self, args: UnloadSkillArgs, ctx: ToolContext) -> UnloadSkillOutput:
        return UnloadSkillOutput(result=_runtime(ctx).skills.unload_skill(ctx.pid, args.skill_id, actor=ctx.pid))


def _runtime(ctx: ToolContext) -> Any:
    if ctx.runtime is None:
        raise ToolExecutionError("Runtime is unavailable.")
    return ctx.runtime

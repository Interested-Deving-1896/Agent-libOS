from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models import ToolSpec
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy

_TOOL_DEFAULTS = DEFAULT_CONFIG.tools


class ProposeJitToolArgs(BaseModel):
    name: str = Field(description="Name of the TypeScript JIT tool to create.")
    description: str = Field(description="Human-readable tool description.")
    source_code: str = Field(description="TypeScript source exporting run(args, libos).")
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    tests: list[dict[str, Any]] = Field(default_factory=list)


class ProposeJitToolOutput(BaseModel):
    candidate_id: str
    name: str
    language: str


class ValidateJitToolArgs(BaseModel):
    candidate_id: str


class ValidateJitToolOutput(BaseModel):
    ok: bool
    errors: list[str]
    warnings: list[str]
    logs: str


class RegisterJitToolArgs(BaseModel):
    candidate_id: str


class RegisterJitToolOutput(BaseModel):
    tool_id: str
    name: str
    scope: str


class ProposeJitTool(SyncAgentTool[ProposeJitToolArgs]):
    name = "propose_jit_tool"
    description = (
        "Propose a Deno/TypeScript JIT tool candidate. The source must export run(args, libos); "
        "libOS access inside the tool happens through libos.syscall()."
    )
    args_schema = ProposeJitToolArgs
    output_schema = ProposeJitToolOutput
    policy = ToolPolicy(side_effects=True, idempotent=False, timeout_s=_TOOL_DEFAULTS.standard_timeout_s)
    tags = ["jit", "tool", "typescript"]

    def run(self, args: ProposeJitToolArgs, ctx: ToolContext) -> ProposeJitToolOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        candidate_id = runtime.tools.propose(
            ctx.pid,
            ToolSpec(
                name=args.name,
                description=args.description,
                input_schema=args.input_schema,
                output_schema=args.output_schema,
                tags=["jit", "typescript"],
                metadata={"language": "typescript"},
            ),
            source_code=args.source_code,
            tests=args.tests,
        )
        return ProposeJitToolOutput(candidate_id=candidate_id, name=args.name, language="typescript")


class ValidateJitTool(SyncAgentTool[ValidateJitToolArgs]):
    name = "validate_jit_tool"
    description = "Validate a proposed Deno/TypeScript JIT tool with static checks and candidate tests."
    args_schema = ValidateJitToolArgs
    output_schema = ValidateJitToolOutput
    policy = ToolPolicy(side_effects=True, idempotent=False, timeout_s=_TOOL_DEFAULTS.standard_timeout_s)
    tags = ["jit", "tool", "typescript", "validation"]

    def run(self, args: ValidateJitToolArgs, ctx: ToolContext) -> ValidateJitToolOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        validation = runtime.tools.validate(args.candidate_id, pid=ctx.pid)
        return ValidateJitToolOutput(
            ok=validation.ok,
            errors=validation.errors,
            warnings=validation.warnings,
            logs=validation.logs,
        )


class RegisterJitTool(SyncAgentTool[RegisterJitToolArgs]):
    name = "register_jit_tool"
    description = "Register a validated Deno/TypeScript JIT tool into the current process tool table."
    args_schema = RegisterJitToolArgs
    output_schema = RegisterJitToolOutput
    policy = ToolPolicy(side_effects=True, idempotent=False, timeout_s=_TOOL_DEFAULTS.standard_timeout_s)
    tags = ["jit", "tool", "typescript", "registration"]

    def run(self, args: RegisterJitToolArgs, ctx: ToolContext) -> RegisterJitToolOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        handle = runtime.tools.register(ctx.pid, args.candidate_id, approver=ctx.pid)
        return RegisterJitToolOutput(tool_id=handle.tool_id, name=handle.name, scope=handle.scope)

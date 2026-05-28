from __future__ import annotations

from pydantic import BaseModel, Field

from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy


class HumanOutputArgs(BaseModel):
    message: str = Field(description="Message to present to the human operator.")
    channel: str = Field(default="terminal", description="Output channel. MVP supports terminal.")


class HumanOutputResult(BaseModel):
    delivered: bool
    channel: str
    chars: int


class HumanOutputTool(SyncAgentTool[HumanOutputArgs]):
    name = "human_output"
    description = (
        "Present a message to the human operator. MVP implementation writes to the terminal. "
        "This is a Skills/Tools Layer wrapper around the libOS HumanObject output primitive; "
        "the primitive enforces human write capability, audit, and events."
    )
    args_schema = HumanOutputArgs
    output_schema = HumanOutputResult
    version = "1.0.0"
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        requires_confirmation=False,
        permissions={"human.output"},
        timeout_s=2.0,
    )
    tags = ["human", "terminal", "output"]

    def run(self, args: HumanOutputArgs, ctx: ToolContext) -> HumanOutputResult:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        result = runtime.human.output(
            pid=ctx.pid,
            message=args.message,
            human="owner",
            channel=args.channel,
        )
        return HumanOutputResult(**result)

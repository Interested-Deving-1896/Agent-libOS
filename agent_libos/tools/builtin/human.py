from __future__ import annotations

from pydantic import BaseModel, Field

from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolPolicy


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
        "This is a Skills/Tools Layer wrapper around HumanObject output."
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
        if args.channel != "terminal":
            # Keep the MVP simple while preserving a future channel field.
            channel = "terminal"
        else:
            channel = args.channel
        print(args.message, flush=True)
        return HumanOutputResult(delivered=True, channel=channel, chars=len(args.message))

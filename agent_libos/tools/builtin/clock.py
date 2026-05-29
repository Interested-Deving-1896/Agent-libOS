from __future__ import annotations

from pydantic import BaseModel, Field

from agent_libos.tools.base import BaseAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy


class GetCurrentTimeArgs(BaseModel):
    timezone: str = Field(default="UTC", description="IANA timezone name, for example UTC or Asia/Shanghai.")


class GetCurrentTimeOutput(BaseModel):
    iso8601: str
    unix_seconds: float
    timezone: str


class SleepArgs(BaseModel):
    seconds: float = Field(ge=0, le=60, description="Seconds to sleep. Maximum is 60 seconds.")


class SleepOutput(BaseModel):
    requested_seconds: float
    elapsed_seconds: float


class GetCurrentTimeTool(BaseAgentTool[GetCurrentTimeArgs]):
    name = "get_current_time"
    description = "Return the current wall-clock time from the libOS clock primitive."
    args_schema = GetCurrentTimeArgs
    output_schema = GetCurrentTimeOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=False, idempotent=False, timeout_s=5.0)
    tags = ["clock", "time"]

    async def execute(self, args: GetCurrentTimeArgs, ctx: ToolContext) -> GetCurrentTimeOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        result = runtime.clock.now(ctx.pid, tz=args.timezone)
        return GetCurrentTimeOutput(
            iso8601=result.iso8601,
            unix_seconds=result.unix_seconds,
            timezone=result.timezone,
        )


class SleepTool(BaseAgentTool[SleepArgs]):
    name = "sleep"
    description = "Sleep for a bounded duration using the libOS clock primitive."
    args_schema = SleepArgs
    output_schema = SleepOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=False, idempotent=False, timeout_s=65.0)
    tags = ["clock", "time", "scheduler"]

    async def execute(self, args: SleepArgs, ctx: ToolContext) -> SleepOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        result = await runtime.clock.asleep(ctx.pid, seconds=args.seconds)
        return SleepOutput(
            requested_seconds=result.requested_seconds,
            elapsed_seconds=result.elapsed_seconds,
        )

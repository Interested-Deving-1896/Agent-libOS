from __future__ import annotations

from pydantic import BaseModel, Field

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.tools.base import BaseAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy

_SHELL_DEFAULTS = DEFAULT_CONFIG.shell
_TOOL_DEFAULTS = DEFAULT_CONFIG.tools


class RunShellCommandArgs(BaseModel):
    argv: list[str] = Field(
        min_length=1,
        description="Command argv array. Shell strings are not accepted.",
    )
    timeout_s: float = Field(
        default=_TOOL_DEFAULTS.shell_timeout_s,
        gt=0,
        le=300,
        description="Command timeout in seconds.",
    )
    max_stdout_chars: int = Field(
        default=_SHELL_DEFAULTS.max_stdout_chars,
        ge=0,
        le=200_000,
        description="Maximum stdout characters returned in the tool result.",
    )
    max_stderr_chars: int = Field(
        default=_SHELL_DEFAULTS.max_stderr_chars,
        ge=0,
        le=200_000,
        description="Maximum stderr characters returned in the tool result.",
    )


class RunShellCommandOutput(BaseModel):
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


class RunShellCommandTool(BaseAgentTool[RunShellCommandArgs]):
    name = "run_shell_command"
    description = (
        "Run an argv-only command through the libOS shell primitive. "
        "The primitive enforces shell execution policy, configured allow/ask lists, human approval, audit, and events."
    )
    args_schema = RunShellCommandArgs
    output_schema = RunShellCommandOutput
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        permissions={"shell.execute"},
        timeout_s=None,
    )
    tags = ["shell", "external", "side_effect"]

    async def execute(self, args: RunShellCommandArgs, ctx: ToolContext) -> RunShellCommandOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        try:
            result = await runtime.shell.arun(ctx.pid, args.argv, timeout=args.timeout_s)
        except TimeoutError as exc:
            raise ToolExecutionError(
                "Shell command timed out.",
                code=ToolErrorCode.TIMEOUT,
                retryable=True,
                details={"argv": args.argv, "timeout_s": args.timeout_s},
            ) from exc
        stdout, stdout_truncated = _truncate(result.stdout, args.max_stdout_chars)
        stderr, stderr_truncated = _truncate(result.stderr, args.max_stderr_chars)
        return RunShellCommandOutput(
            argv=result.argv,
            returncode=result.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


def _truncate(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True

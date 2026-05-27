from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolPolicy


class EchoArgs(BaseModel):
    model_config = ConfigDict(extra="allow")


class EchoTool(SyncAgentTool[EchoArgs]):
    name = "echo"
    description = "Return the provided arguments unchanged. Useful for tool plumbing tests."
    args_schema = EchoArgs
    version = "1.0.0"
    policy = ToolPolicy(side_effects=False, idempotent=True, timeout_s=2.0)
    tags = ["debug", "deterministic"]

    def run(self, args: EchoArgs, ctx: ToolContext) -> dict[str, Any]:
        return args.model_dump()


class ParsePytestLogArgs(BaseModel):
    log: str = Field(description="Raw pytest output.")


class ParsePytestLogOutput(BaseModel):
    failed: list[str]
    errors: list[str]
    assertions: list[str]
    failure_count: int


class ParsePytestLogTool(SyncAgentTool[ParsePytestLogArgs]):
    name = "parse_pytest_log"
    description = "Parse pytest output into a small structured failure summary."
    args_schema = ParsePytestLogArgs
    output_schema = ParsePytestLogOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=False, idempotent=True, timeout_s=2.0)
    tags = ["coding", "pytest", "parser"]

    def run(self, args: ParsePytestLogArgs, ctx: ToolContext) -> ParsePytestLogOutput:
        failed: list[str] = []
        errors: list[str] = []
        assertions: list[str] = []
        for line in args.log.splitlines():
            stripped = line.strip()
            if stripped.startswith("FAILED "):
                failed.append(stripped)
            elif re.match(r"^E\s+", stripped):
                errors.append(stripped[2:])
            elif "AssertionError" in stripped:
                assertions.append(stripped)
        return ParsePytestLogOutput(
            failed=failed,
            errors=errors,
            assertions=assertions,
            failure_count=len(failed) or len(assertions) or len(errors),
        )


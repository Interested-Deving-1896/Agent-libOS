from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agent_libos.models import ObjectHandle, ObjectMetadata, ObjectType
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy


class ProcessExitArgs(BaseModel):
    payload: dict[str, Any] | None = Field(default=None, description="Optional structured final result.")
    result_oid: str | None = Field(default=None, description="Existing object id to use as process result.")
    message: str | None = Field(default=None, description="Optional status message.")

    @field_validator("payload", mode="before")
    @classmethod
    def parse_json_payload(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return {"content": value}
            if isinstance(decoded, dict):
                return decoded
            return {"value": decoded}
        return value


class ProcessExitOutput(BaseModel):
    status: str
    result_oid: str | None = None


class ProcessExitTool(SyncAgentTool[ProcessExitArgs]):
    name = "process_exit"
    description = (
        "Exit the current Agent Process with an optional final result. "
        "This is a Skills/Tools Layer wrapper over process lifecycle primitives."
    )
    args_schema = ProcessExitArgs
    output_schema = ProcessExitOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=False, idempotent=True, timeout_s=5.0)
    tags = ["process", "lifecycle"]

    def run(self, args: ProcessExitArgs, ctx: ToolContext) -> ProcessExitOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        result_handle: ObjectHandle | None = None
        if args.result_oid:
            result_handle = runtime.capability.handle_for_object(
                ctx.pid,
                args.result_oid,
                {"read", "materialize", "link", "diff"},
                issued_by="process_exit_tool",
            )
        elif args.payload is not None:
            result_handle = runtime.memory.create_object(
                pid=ctx.pid,
                object_type=ObjectType.SUMMARY,
                payload=args.payload,
                metadata=ObjectMetadata(title="Process final result", tags=["final"]),
            )
        runtime.process.exit(ctx.pid, result=result_handle, message=args.message)
        result_oid = result_handle.oid if result_handle is not None else None
        return ProcessExitOutput(status="exited", result_oid=result_oid)

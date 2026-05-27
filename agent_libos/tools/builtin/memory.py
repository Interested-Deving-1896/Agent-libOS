from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent_libos.models import ObjectMetadata, ObjectType, ViewMode
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy


class CreateMemoryObjectArgs(BaseModel):
    type: str = Field(description="Agent libOS object type, for example summary, plan, observation, or artifact.")
    payload: Any = Field(description="Structured payload to store.")
    metadata: dict[str, Any] = Field(default_factory=dict)
    immutable: bool = True


class CreateMemoryObjectOutput(BaseModel):
    oid: str
    type: str


class CreateMemoryObjectTool(SyncAgentTool[CreateMemoryObjectArgs]):
    name = "create_memory_object"
    description = (
        "Create a typed object in Agent libOS Object Memory and attach it to this process MemoryView. "
        "This is a Skills/Tools Layer wrapper over the memory manager."
    )
    args_schema = CreateMemoryObjectArgs
    output_schema = CreateMemoryObjectOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=False, idempotent=False, timeout_s=5.0)
    tags = ["memory", "object"]

    def run(self, args: CreateMemoryObjectArgs, ctx: ToolContext) -> CreateMemoryObjectOutput:
        if ctx.runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        metadata = ObjectMetadata(
            title=args.metadata.get("title"),
            summary=args.metadata.get("summary"),
            tags=args.metadata.get("tags", []),
            mime_type=args.metadata.get("mime_type"),
            sensitivity=args.metadata.get("sensitivity", "normal"),
            retention_policy=args.metadata.get("retention_policy", "default"),
        )
        handle = ctx.runtime.memory.create_object(
            pid=ctx.pid,
            type=ObjectType(args.type),
            payload=args.payload,
            metadata=metadata,
            immutable=args.immutable,
        )
        process = ctx.runtime.process.get(ctx.pid)
        if process.memory_view is None:
            process.memory_view = ctx.runtime.memory.create_view(ctx.pid, [handle], mode=ViewMode.READ_ONLY)
        elif all(existing.oid != handle.oid for existing in process.memory_view.roots):
            process.memory_view.roots.append(handle)
        ctx.runtime.store.update_process(process)
        return CreateMemoryObjectOutput(oid=handle.oid, type=args.type)


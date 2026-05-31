from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent_libos.models import ObjectMetadata, ObjectPatch, ObjectType, ViewMode
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy


class CreateMemoryObjectArgs(BaseModel):
    name: str | None = Field(default=None, description="Optional namespace-local object name.")
    namespace: str | None = Field(default=None, description="Object Memory namespace. Defaults to this process namespace.")
    type: str = Field(description="Agent libOS object type, for example summary, plan, observation, or artifact.")
    payload: Any = Field(description="Structured payload to store.")
    metadata: dict[str, Any] = Field(default_factory=dict)
    immutable: bool = True


class CreateMemoryObjectOutput(BaseModel):
    oid: str
    namespace: str
    name: str
    type: str


class ReadMemoryObjectArgs(BaseModel):
    name: str = Field(description="Namespace-local Object Memory name to read.")
    namespace: str | None = Field(default=None, description="Object Memory namespace. Defaults to this process namespace.")
    max_payload_chars: int = Field(default=12000, ge=1, le=200000, description="Maximum rendered payload chars.")


class ReadMemoryObjectOutput(BaseModel):
    oid: str
    namespace: str
    name: str
    type: str
    version: int
    payload: Any
    truncated: bool


class AppendMemoryObjectArgs(BaseModel):
    name: str = Field(description="Namespace-local mutable Object Memory name to append to.")
    namespace: str | None = Field(default=None, description="Object Memory namespace. Defaults to this process namespace.")
    entry: Any = Field(description="Structured entry to append.")
    list_field: str = Field(
        default="entries",
        description="Payload list field to append into when the object payload is a JSON object.",
    )


class AppendMemoryObjectOutput(BaseModel):
    oid: str
    namespace: str
    name: str
    version: int
    appended: bool
    list_field: str | None = None
    length: int


class CreateMemoryNamespaceArgs(BaseModel):
    namespace: str = Field(description="Namespace path to create, for example project/research or child-results.")
    parent_namespace: str | None = Field(
        default=None,
        description="Parent namespace. Defaults to the path parent; top-level namespaces have no parent.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateMemoryNamespaceOutput(BaseModel):
    namespace: str
    parent_namespace: str | None
    created: bool


class ListMemoryNamespaceArgs(BaseModel):
    namespace: str | None = Field(default=None, description="Namespace to list. Defaults to this process namespace.")


class MemoryNamespaceObjectEntry(BaseModel):
    oid: str
    namespace: str
    name: str
    type: str
    version: int


class MemoryNamespaceEntry(BaseModel):
    namespace: str
    parent_namespace: str | None


class ListMemoryNamespaceOutput(BaseModel):
    namespace: str
    objects: list[MemoryNamespaceObjectEntry]
    namespaces: list[MemoryNamespaceEntry]


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
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        metadata = ObjectMetadata(
            title=args.metadata.get("title"),
            summary=args.metadata.get("summary"),
            tags=args.metadata.get("tags", []),
            mime_type=args.metadata.get("mime_type"),
            sensitivity=args.metadata.get("sensitivity", "normal"),
            retention_policy=args.metadata.get("retention_policy", "default"),
        )
        handle = runtime.memory.create_object(
            pid=ctx.pid,
            object_type=ObjectType(args.type),
            payload=args.payload,
            metadata=metadata,
            immutable=args.immutable,
            name=args.name,
            namespace=args.namespace,
        )
        obj = runtime.memory.get_object(ctx.pid, handle)
        process = runtime.process.get(ctx.pid)
        if process.memory_view is None:
            process.memory_view = runtime.memory.create_view(ctx.pid, [handle], mode=ViewMode.READ_ONLY)
        elif all(existing.oid != handle.oid for existing in process.memory_view.roots):
            process.memory_view.roots.append(handle)
        runtime.store.update_process(process)
        return CreateMemoryObjectOutput(oid=handle.oid, namespace=obj.namespace, name=obj.name, type=args.type)


class CreateMemoryNamespaceTool(SyncAgentTool[CreateMemoryNamespaceArgs]):
    name = "create_memory_namespace"
    description = (
        "Create an Object Memory namespace. Namespaces provide directory-like name scopes; "
        "object capabilities still control object reads and writes."
    )
    args_schema = CreateMemoryNamespaceArgs
    output_schema = CreateMemoryNamespaceOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=True, idempotent=False, timeout_s=5.0)
    tags = ["memory", "object", "namespace"]

    def run(self, args: CreateMemoryNamespaceArgs, ctx: ToolContext) -> CreateMemoryNamespaceOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        namespace = runtime.memory.create_namespace(
            pid=ctx.pid,
            namespace=args.namespace,
            parent_namespace=args.parent_namespace,
            metadata=args.metadata,
        )
        return CreateMemoryNamespaceOutput(
            namespace=namespace.namespace,
            parent_namespace=namespace.parent_namespace,
            created=True,
        )


class ListMemoryNamespaceTool(SyncAgentTool[ListMemoryNamespaceArgs]):
    name = "list_memory_namespace"
    description = (
        "List process-visible objects and child namespaces within an Object Memory namespace. "
        "The list contains only objects the process can read."
    )
    args_schema = ListMemoryNamespaceArgs
    output_schema = ListMemoryNamespaceOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=False, idempotent=True, timeout_s=5.0)
    tags = ["memory", "object", "namespace", "read"]

    def run(self, args: ListMemoryNamespaceArgs, ctx: ToolContext) -> ListMemoryNamespaceOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        listing = runtime.memory.list_namespace(ctx.pid, args.namespace)
        objects = [
            MemoryNamespaceObjectEntry(
                oid=obj.oid,
                namespace=obj.namespace,
                name=obj.name,
                type=obj.type.value,
                version=obj.version,
            )
            for obj in listing["objects"]
        ]
        namespaces = [
            MemoryNamespaceEntry(namespace=namespace.namespace, parent_namespace=namespace.parent_namespace)
            for namespace in listing["namespaces"]
        ]
        return ListMemoryNamespaceOutput(
            namespace=listing["namespace"],
            objects=objects,
            namespaces=namespaces,
        )


class ReadMemoryObjectTool(SyncAgentTool[ReadMemoryObjectArgs]):
    name = "read_memory_object"
    description = (
        "Read a named Object Memory object. Name lookup does not grant authority; "
        "the memory primitive still enforces object read capability."
    )
    args_schema = ReadMemoryObjectArgs
    output_schema = ReadMemoryObjectOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=False, idempotent=True, timeout_s=5.0)
    tags = ["memory", "object", "read"]

    def run(self, args: ReadMemoryObjectArgs, ctx: ToolContext) -> ReadMemoryObjectOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        obj = runtime.memory.get_object_by_name(ctx.pid, args.name, namespace=args.namespace)
        payload = obj.payload
        rendered = repr(payload)
        truncated = len(rendered) > args.max_payload_chars
        if truncated:
            payload = rendered[: args.max_payload_chars]
        return ReadMemoryObjectOutput(
            oid=obj.oid,
            namespace=obj.namespace,
            name=obj.name,
            type=obj.type.value,
            version=obj.version,
            payload=payload,
            truncated=truncated,
        )


class AppendMemoryObjectTool(SyncAgentTool[AppendMemoryObjectArgs]):
    name = "append_memory_object"
    description = (
        "Append a structured entry to a mutable named Object Memory object. "
        "This is the preferred write pattern for LLM context objects because it preserves prompt-cache-friendly prefixes."
    )
    args_schema = AppendMemoryObjectArgs
    output_schema = AppendMemoryObjectOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=True, idempotent=False, timeout_s=5.0)
    tags = ["memory", "object", "write", "append"]

    def run(self, args: AppendMemoryObjectArgs, ctx: ToolContext) -> AppendMemoryObjectOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        handle = runtime.memory.handle_for_name(
            ctx.pid,
            args.name,
            rights=["read", "write"],
            issued_by="append_memory_object_tool",
            namespace=args.namespace,
        )
        obj = runtime.memory.get_object(ctx.pid, handle)
        payload = obj.payload
        if isinstance(payload, dict):
            values = payload.setdefault(args.list_field, [])
            if not isinstance(values, list):
                raise ToolExecutionError(
                    "Target payload field is not a list.",
                    code=ToolErrorCode.VALIDATION_ERROR,
                    details={"name": args.name, "list_field": args.list_field},
                )
            values.append(args.entry)
            length = len(values)
            list_field: str | None = args.list_field
        elif isinstance(payload, list):
            payload.append(args.entry)
            length = len(payload)
            list_field = None
        else:
            raise ToolExecutionError(
                "Target object payload is not appendable.",
                code=ToolErrorCode.VALIDATION_ERROR,
                details={"name": args.name, "payload_type": type(payload).__name__},
            )
        runtime.memory.update_object(ctx.pid, handle, ObjectPatch(payload=payload))
        updated = runtime.memory.get_object(ctx.pid, handle)
        return AppendMemoryObjectOutput(
            oid=updated.oid,
            namespace=updated.namespace,
            name=updated.name,
            version=updated.version,
            appended=True,
            list_field=list_field,
            length=length,
        )

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent_libos.models import ObjectMetadata, ObjectType
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy


class CreateObjectFromFileArgs(BaseModel):
    name: str = Field(description="Globally unique Object Memory name to create.")
    path: str = Field(description="Workspace-relative file path to import.")
    encoding: str = Field(default="utf-8", description="Text encoding.")
    max_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760, description="Maximum bytes to import.")
    allow_truncated: bool = Field(default=False, description="Whether to create the object if the file is truncated.")
    object_type: str = Field(default=ObjectType.ARTIFACT.value, description="ObjectType for the created object.")


class CreateObjectFromFileOutput(BaseModel):
    oid: str
    name: str
    type: str
    source_path: str
    bytes_read: int
    truncated: bool


class WriteObjectToFileArgs(BaseModel):
    name: str = Field(description="Object Memory name to resolve and write.")
    path: str = Field(description="Workspace-relative output file path.")
    encoding: str = Field(default="utf-8", description="Text encoding.")
    overwrite: bool = Field(default=True, description="Whether to overwrite an existing file.")


class WriteObjectToFileOutput(BaseModel):
    oid: str
    name: str
    path: str
    bytes_written: int
    created: bool


class CreateObjectFromFileTool(SyncAgentTool[CreateObjectFromFileArgs]):
    name = "create_object_from_file"
    description = (
        "Create a named Object Memory object whose payload is the text content of a workspace file. "
        "The file content is stored inside Object Memory but is not returned in the tool result."
    )
    args_schema = CreateObjectFromFileArgs
    output_schema = CreateObjectFromFileOutput
    version = "1.0.0"
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        permissions={"filesystem.read", "object.write"},
        timeout_s=5.0,
    )
    tags = ["memory", "filesystem", "object"]

    def run(self, args: CreateObjectFromFileArgs, ctx: ToolContext) -> CreateObjectFromFileOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        try:
            result = runtime.filesystem.read_text(
                pid=ctx.pid,
                path=args.path,
                encoding=args.encoding,
                max_bytes=args.max_bytes,
            )
        except UnicodeDecodeError as exc:
            raise ToolExecutionError(
                "File could not be decoded with the requested encoding.",
                code=ToolErrorCode.EXECUTION_ERROR,
                details={"encoding": args.encoding, "path": args.path},
            ) from exc
        if result.truncated and not args.allow_truncated:
            raise ToolExecutionError(
                "File exceeded max_bytes; no object was created.",
                code=ToolErrorCode.EXECUTION_ERROR,
                details={"path": result.path, "bytes_read": result.bytes_read, "max_bytes": args.max_bytes},
            )
        # The content moves into Object Memory, but the tool result exposes only
        # metadata so a process can copy files without seeing the bytes.
        payload = {
            "kind": "workspace_text_file",
            "source_path": result.path,
            "encoding": args.encoding,
            "content": result.content,
            "bytes_read": result.bytes_read,
            "truncated": result.truncated,
        }
        handle = runtime.memory.create_object(
            pid=ctx.pid,
            object_type=ObjectType(args.object_type),
            payload=payload,
            metadata=ObjectMetadata(
                title=args.name,
                tags=["file_object", "workspace_file"],
                mime_type="text/plain",
                token_estimate=0,
            ),
            immutable=True,
            name=args.name,
        )
        obj = runtime.memory.get_object(ctx.pid, handle)
        return CreateObjectFromFileOutput(
            oid=handle.oid,
            name=obj.name,
            type=args.object_type,
            source_path=result.path,
            bytes_read=result.bytes_read,
            truncated=result.truncated,
        )


class WriteObjectToFileTool(SyncAgentTool[WriteObjectToFileArgs]):
    name = "write_object_to_file"
    description = (
        "Resolve a named Object Memory object and write its text content to a workspace file. "
        "The object content is not returned in the tool result."
    )
    args_schema = WriteObjectToFileArgs
    output_schema = WriteObjectToFileOutput
    version = "1.0.0"
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        permissions={"filesystem.write", "object.read"},
        timeout_s=5.0,
    )
    tags = ["memory", "filesystem", "object", "side_effect"]

    def run(self, args: WriteObjectToFileArgs, ctx: ToolContext) -> WriteObjectToFileOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        obj = runtime.memory.get_object_by_name(ctx.pid, args.name)
        text = self._extract_text(obj.payload)
        # The object payload is handed directly to the filesystem primitive; the
        # process-visible result below still omits the concrete content.
        try:
            result = runtime.filesystem.write_text(
                pid=ctx.pid,
                path=args.path,
                text=text,
                encoding=args.encoding,
                overwrite=args.overwrite,
            )
        except FileExistsError as exc:
            raise ToolExecutionError(
                "File already exists and overwrite is false.",
                code=ToolErrorCode.EXECUTION_ERROR,
                details={"path": args.path},
            ) from exc
        return WriteObjectToFileOutput(
            oid=obj.oid,
            name=obj.name,
            path=result.path,
            bytes_written=result.bytes_written,
            created=result.created,
        )

    def _extract_text(self, payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("content"), str):
            return payload["content"]
        raise ToolExecutionError(
            "Object payload does not contain text content.",
            code=ToolErrorCode.EXECUTION_ERROR,
            details={"expected": "string payload or dict content string"},
        )

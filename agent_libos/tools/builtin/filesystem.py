from __future__ import annotations

from pydantic import BaseModel, Field

from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy


class WriteTextFileArgs(BaseModel):
    path: str = Field(description="Relative file path under the runtime workspace root.")
    content: str = Field(description="Exact UTF-8 text content to write.")
    encoding: str = Field(default="utf-8", description="Text encoding.")
    overwrite: bool = Field(default=True, description="Whether to overwrite an existing file.")


class WriteTextFileOutput(BaseModel):
    path: str
    bytes_written: int
    created: bool


class ReadTextFileArgs(BaseModel):
    path: str = Field(description="Relative file path under the runtime workspace root.")
    encoding: str = Field(default="utf-8", description="Text encoding.")
    max_bytes: int = Field(default=65536, ge=1, le=1048576, description="Maximum bytes to read.")


class ReadTextFileOutput(BaseModel):
    path: str
    content: str
    bytes_read: int
    truncated: bool


class ReadTextFileTool(SyncAgentTool[ReadTextFileArgs]):
    name = "read_text_file"
    description = (
        "Read UTF-8 text from a file under the runtime workspace root. "
        "This is a Skills/Tools Layer wrapper around the libOS filesystem primitive; "
        "the primitive enforces filesystem read capability, path containment, audit, and events."
    )
    args_schema = ReadTextFileArgs
    output_schema = ReadTextFileOutput
    version = "1.0.0"
    policy = ToolPolicy(
        side_effects=False,
        idempotent=True,
        requires_confirmation=False,
        permissions={"filesystem.read"},
        timeout_s=5.0,
    )
    tags = ["filesystem", "workspace", "read"]

    def run(self, args: ReadTextFileArgs, ctx: ToolContext) -> ReadTextFileOutput:
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
                details={"encoding": args.encoding, "error": str(exc)},
            ) from exc
        return ReadTextFileOutput(
            path=result.path,
            content=result.content,
            bytes_read=result.bytes_read,
            truncated=result.truncated,
        )


class WriteTextFileTool(SyncAgentTool[WriteTextFileArgs]):
    name = "write_text_file"
    description = (
        "Write UTF-8 text to a file under the runtime workspace root. "
        "This is a Skills/Tools Layer wrapper around the libOS filesystem primitive; "
        "the primitive enforces filesystem write capability, path containment, audit, and events."
    )
    args_schema = WriteTextFileArgs
    output_schema = WriteTextFileOutput
    version = "1.0.0"
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        requires_confirmation=True,
        permissions={"filesystem.write"},
        timeout_s=5.0,
    )
    tags = ["filesystem", "workspace", "side_effect"]

    def run(self, args: WriteTextFileArgs, ctx: ToolContext) -> WriteTextFileOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        try:
            result = runtime.filesystem.write_text(
                pid=ctx.pid,
                path=args.path,
                text=args.content,
                encoding=args.encoding,
                overwrite=args.overwrite,
            )
        except FileExistsError as exc:
            raise ToolExecutionError(
                "File already exists and overwrite is false.",
                code=ToolErrorCode.EXECUTION_ERROR,
                details={"path": args.path},
            ) from exc
        return WriteTextFileOutput(
            path=result.path,
            bytes_written=result.bytes_written,
            created=result.created,
        )

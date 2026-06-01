from __future__ import annotations

from pydantic import BaseModel, Field

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy

_TOOL_DEFAULTS = DEFAULT_CONFIG.tools


class WriteTextFileArgs(BaseModel):
    path: str = Field(description="Relative file path under the runtime workspace root.")
    content: str = Field(description="Exact UTF-8 text content to write.")
    encoding: str = Field(default=_TOOL_DEFAULTS.default_text_encoding, description="Text encoding.")
    overwrite: bool = Field(default=True, description="Whether to overwrite an existing file.")


class WriteTextFileOutput(BaseModel):
    path: str
    bytes_written: int
    created: bool


class ReadTextFileArgs(BaseModel):
    path: str = Field(description="Relative file path under the runtime workspace root.")
    encoding: str = Field(default=_TOOL_DEFAULTS.default_text_encoding, description="Text encoding.")
    max_bytes: int = Field(
        default=_TOOL_DEFAULTS.filesystem_read_max_bytes,
        ge=1,
        le=_TOOL_DEFAULTS.filesystem_read_hard_limit_bytes,
        description="Maximum bytes to read.",
    )


class ReadTextFileOutput(BaseModel):
    path: str
    content: str
    bytes_read: int
    truncated: bool


class DirectoryEntryOutput(BaseModel):
    name: str
    path: str
    kind: str
    size_bytes: int | None
    modified_at: str


class ReadDirectoryArgs(BaseModel):
    path: str = Field(description="Relative directory path under the runtime workspace root.")
    limit: int = Field(
        default=_TOOL_DEFAULTS.directory_entry_limit,
        ge=1,
        le=_TOOL_DEFAULTS.directory_entry_hard_limit,
        description="Maximum number of entries to return.",
    )


class ReadDirectoryOutput(BaseModel):
    path: str
    entries: list[DirectoryEntryOutput]
    count: int
    truncated: bool


class WriteDirectoryArgs(BaseModel):
    path: str = Field(description="Relative directory path under the runtime workspace root.")
    parents: bool = Field(default=True, description="Whether to create missing parent directories.")
    exist_ok: bool = Field(default=True, description="Whether an existing directory is accepted.")


class WriteDirectoryOutput(BaseModel):
    path: str
    created: bool


class DeleteFileArgs(BaseModel):
    path: str = Field(description="Relative file path under the runtime workspace root.")
    missing_ok: bool = Field(default=False, description="Whether a missing file should be treated as success.")


class DeleteDirectoryArgs(BaseModel):
    path: str = Field(description="Relative directory path under the runtime workspace root.")
    recursive: bool = Field(default=False, description="Whether to delete a non-empty directory recursively.")
    missing_ok: bool = Field(default=False, description="Whether a missing directory should be treated as success.")


class DeletePathOutput(BaseModel):
    path: str
    kind: str
    deleted: bool
    recursive: bool = False


class ReadTextFileTool(SyncAgentTool[ReadTextFileArgs]):
    name = "read_text_file"
    description = (
        "Read UTF-8 text from a file under the runtime workspace root. "
        "This is a Skills/Tools Layer wrapper around the libOS filesystem primitive; "
        "the primitive enforces filesystem read capability, path containment, audit, and events."
    )
    args_schema = ReadTextFileArgs
    output_schema = ReadTextFileOutput
    policy = ToolPolicy(
        side_effects=False,
        idempotent=True,
        requires_confirmation=False,
        permissions={"filesystem.read"},
        timeout_s=_TOOL_DEFAULTS.standard_timeout_s,
    )
    tags = ["filesystem", "workspace", "read"]

    def run(self, args: ReadTextFileArgs, ctx: ToolContext) -> ReadTextFileOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        cwd = runtime.process.working_directory(ctx.pid)
        try:
            result = runtime.filesystem.read_text(
                pid=ctx.pid,
                path=args.path,
                encoding=args.encoding,
                max_bytes=args.max_bytes,
                cwd=cwd,
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


class ReadDirectoryTool(SyncAgentTool[ReadDirectoryArgs]):
    name = "read_directory"
    description = (
        "List entries in a directory under the runtime workspace root. "
        "The filesystem primitive enforces directory read capability, path containment, audit, and events."
    )
    args_schema = ReadDirectoryArgs
    output_schema = ReadDirectoryOutput
    policy = ToolPolicy(
        side_effects=False,
        idempotent=True,
        requires_confirmation=False,
        permissions={"filesystem.read"},
        timeout_s=_TOOL_DEFAULTS.standard_timeout_s,
    )
    tags = ["filesystem", "workspace", "read", "directory"]

    def run(self, args: ReadDirectoryArgs, ctx: ToolContext) -> ReadDirectoryOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        cwd = runtime.process.working_directory(ctx.pid)
        result = runtime.filesystem.read_directory(
            pid=ctx.pid,
            path=args.path,
            limit=args.limit,
            cwd=cwd,
        )
        return ReadDirectoryOutput(
            path=result.path,
            entries=[DirectoryEntryOutput(**entry.__dict__) for entry in result.entries],
            count=result.count,
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
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        requires_confirmation=True,
        permissions={"filesystem.write"},
        timeout_s=_TOOL_DEFAULTS.standard_timeout_s,
    )
    tags = ["filesystem", "workspace", "side_effect"]

    def run(self, args: WriteTextFileArgs, ctx: ToolContext) -> WriteTextFileOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        cwd = runtime.process.working_directory(ctx.pid)
        try:
            result = runtime.filesystem.write_text(
                pid=ctx.pid,
                path=args.path,
                text=args.content,
                encoding=args.encoding,
                overwrite=args.overwrite,
                cwd=cwd,
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


class WriteDirectoryTool(SyncAgentTool[WriteDirectoryArgs]):
    name = "write_directory"
    description = (
        "Create or ensure a directory under the runtime workspace root. "
        "The filesystem primitive enforces directory write capability, path containment, audit, and events."
    )
    args_schema = WriteDirectoryArgs
    output_schema = WriteDirectoryOutput
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        requires_confirmation=True,
        permissions={"filesystem.write"},
        timeout_s=_TOOL_DEFAULTS.standard_timeout_s,
    )
    tags = ["filesystem", "workspace", "side_effect", "directory"]

    def run(self, args: WriteDirectoryArgs, ctx: ToolContext) -> WriteDirectoryOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        cwd = runtime.process.working_directory(ctx.pid)
        try:
            result = runtime.filesystem.write_directory(
                pid=ctx.pid,
                path=args.path,
                parents=args.parents,
                exist_ok=args.exist_ok,
                cwd=cwd,
            )
        except FileExistsError as exc:
            raise ToolExecutionError(
                "Directory already exists and exist_ok is false.",
                code=ToolErrorCode.EXECUTION_ERROR,
                details={"path": args.path},
            ) from exc
        return WriteDirectoryOutput(path=result.path, created=result.created)


class DeleteFileTool(SyncAgentTool[DeleteFileArgs]):
    name = "delete_file"
    description = (
        "Delete a file under the runtime workspace root. "
        "The filesystem primitive enforces delete capability, path containment, audit, and events."
    )
    args_schema = DeleteFileArgs
    output_schema = DeletePathOutput
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        requires_confirmation=True,
        permissions={"filesystem.delete"},
        timeout_s=_TOOL_DEFAULTS.standard_timeout_s,
    )
    tags = ["filesystem", "workspace", "side_effect", "delete"]

    def run(self, args: DeleteFileArgs, ctx: ToolContext) -> DeletePathOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        cwd = runtime.process.working_directory(ctx.pid)
        result = runtime.filesystem.delete_file(
            pid=ctx.pid,
            path=args.path,
            missing_ok=args.missing_ok,
            cwd=cwd,
        )
        return DeletePathOutput(
            path=result.path,
            kind=result.kind,
            deleted=result.deleted,
            recursive=result.recursive,
        )


class DeleteDirectoryTool(SyncAgentTool[DeleteDirectoryArgs]):
    name = "delete_directory"
    description = (
        "Delete a directory under the runtime workspace root. "
        "The filesystem primitive enforces delete capability, path containment, audit, and events."
    )
    args_schema = DeleteDirectoryArgs
    output_schema = DeletePathOutput
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        requires_confirmation=True,
        permissions={"filesystem.delete"},
        timeout_s=_TOOL_DEFAULTS.standard_timeout_s,
    )
    tags = ["filesystem", "workspace", "side_effect", "delete", "directory"]

    def run(self, args: DeleteDirectoryArgs, ctx: ToolContext) -> DeletePathOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        cwd = runtime.process.working_directory(ctx.pid)
        try:
            result = runtime.filesystem.delete_directory(
                pid=ctx.pid,
                path=args.path,
                recursive=args.recursive,
                missing_ok=args.missing_ok,
                cwd=cwd,
            )
        except OSError as exc:
            raise ToolExecutionError(
                "Directory could not be deleted.",
                code=ToolErrorCode.EXECUTION_ERROR,
                details={"path": args.path, "error": str(exc)},
            ) from exc
        return DeletePathOutput(
            path=result.path,
            kind=result.kind,
            deleted=result.deleted,
            recursive=result.recursive,
        )

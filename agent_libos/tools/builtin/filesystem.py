from __future__ import annotations

from pathlib import Path

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
        "This is a Skills/Tools Layer wrapper around workspace filesystem reads."
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
        workspace = _workspace(ctx)
        target = _resolve_workspace_path(workspace, args.path)
        if not target.exists():
            raise ToolExecutionError(
                "File does not exist.",
                code=ToolErrorCode.EXECUTION_ERROR,
                details={"path": str(target.relative_to(workspace))},
            )
        if not target.is_file():
            raise ToolExecutionError(
                "Path is not a file.",
                code=ToolErrorCode.EXECUTION_ERROR,
                details={"path": str(target.relative_to(workspace))},
            )
        raw = target.read_bytes()
        truncated = len(raw) > args.max_bytes
        selected = raw[: args.max_bytes]
        try:
            content = selected.decode(args.encoding)
        except UnicodeDecodeError as exc:
            raise ToolExecutionError(
                "File could not be decoded with the requested encoding.",
                code=ToolErrorCode.EXECUTION_ERROR,
                details={"encoding": args.encoding, "error": str(exc)},
            ) from exc
        return ReadTextFileOutput(
            path=str(target.relative_to(workspace)),
            content=content,
            bytes_read=len(selected),
            truncated=truncated,
        )


class WriteTextFileTool(SyncAgentTool[WriteTextFileArgs]):
    name = "write_text_file"
    description = (
        "Write UTF-8 text to a file under the runtime workspace root. "
        "This is a Skills/Tools Layer wrapper around filesystem side effects, not a kernel syscall."
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
        workspace = _workspace(ctx)
        target = _resolve_workspace_path(workspace, args.path)
        created = not target.exists()
        if target.exists() and not args.overwrite:
            raise ToolExecutionError(
                "File already exists and overwrite is false.",
                code=ToolErrorCode.EXECUTION_ERROR,
                details={"path": str(target.relative_to(workspace))},
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args.content, encoding=args.encoding)
        return WriteTextFileOutput(
            path=str(target.relative_to(workspace)),
            bytes_written=len(args.content.encode(args.encoding)),
            created=created,
        )


def _workspace(ctx: ToolContext) -> Path:
    if ctx.workspace_id is None:
        raise ToolExecutionError(
            "No workspace is available for filesystem access.",
            code=ToolErrorCode.PERMISSION_DENIED,
        )
    return Path(ctx.workspace_id).resolve()


def _resolve_workspace_path(workspace: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        raise ToolExecutionError(
            "Path must be relative to the workspace root.",
            code=ToolErrorCode.PERMISSION_DENIED,
        )
    target = (workspace / path).resolve()
    if target != workspace and workspace not in target.parents:
        raise ToolExecutionError(
            "Path escapes workspace.",
            code=ToolErrorCode.PERMISSION_DENIED,
            details={"path": raw_path},
        )
    return target

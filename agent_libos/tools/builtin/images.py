from __future__ import annotations

from pydantic import BaseModel, Field

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models.exceptions import ValidationError as LibOSValidationError
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy

_IMAGE_DEFAULTS = DEFAULT_CONFIG.image
_TOOL_DEFAULTS = DEFAULT_CONFIG.tools


class LoadImageFromYamlArgs(BaseModel):
    path: str = Field(description="Workspace-relative YAML file path containing one AgentImage manifest.")
    encoding: str = Field(default=_TOOL_DEFAULTS.default_text_encoding, description="Text encoding.")
    max_bytes: int = Field(
        default=_IMAGE_DEFAULTS.yaml_max_bytes,
        ge=1,
        le=_IMAGE_DEFAULTS.yaml_hard_limit_bytes,
        description="Maximum YAML bytes to read.",
    )
    replace: bool = Field(default=False, description="Whether an existing image with the same id may be replaced.")


class LoadImageFromYamlOutput(BaseModel):
    image_id: str
    name: str
    version: str
    source_path: str
    replaced: bool
    default_tools: list[str]
    required_capabilities_count: int


class LoadImageFromYamlTool(SyncAgentTool[LoadImageFromYamlArgs]):
    name = "load_image_from_yaml"
    description = (
        "Read an AgentImage registration manifest from a workspace YAML file and register it with the runtime. "
        "The filesystem primitive enforces file read authority; the image registry primitive enforces image write authority."
    )
    args_schema = LoadImageFromYamlArgs
    output_schema = LoadImageFromYamlOutput
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        permissions={"filesystem.read", "image.write"},
        timeout_s=_TOOL_DEFAULTS.standard_timeout_s,
    )
    tags = ["image", "registry", "yaml", "side_effect"]

    def run(self, args: LoadImageFromYamlArgs, ctx: ToolContext) -> LoadImageFromYamlOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        cwd = runtime.process.working_directory(ctx.pid)
        try:
            file_result = runtime.filesystem.read_text(
                pid=ctx.pid,
                path=args.path,
                encoding=args.encoding,
                max_bytes=args.max_bytes,
                cwd=cwd,
            )
        except UnicodeDecodeError as exc:
            raise ToolExecutionError(
                "Image YAML file could not be decoded with the requested encoding.",
                code=ToolErrorCode.EXECUTION_ERROR,
                details={"encoding": args.encoding, "path": args.path},
            ) from exc
        if file_result.truncated:
            raise ToolExecutionError(
                "Image YAML exceeded max_bytes; no image was registered.",
                code=ToolErrorCode.VALIDATION_ERROR,
                details={"path": file_result.path, "bytes_read": file_result.bytes_read, "max_bytes": args.max_bytes},
            )
        try:
            result = runtime.image_registry.register_from_yaml_text(
                file_result.content,
                actor=ctx.pid,
                replace=args.replace,
                require_capability=True,
                source=file_result.path,
            )
        except LibOSValidationError as exc:
            raise ToolExecutionError(
                str(exc),
                code=ToolErrorCode.VALIDATION_ERROR,
                details={"path": file_result.path},
            ) from exc
        image = result.image
        return LoadImageFromYamlOutput(
            image_id=image.image_id,
            name=image.name,
            version=image.version,
            source_path=file_result.path,
            replaced=result.replaced,
            default_tools=list(image.default_tools),
            required_capabilities_count=len(image.required_capabilities),
        )

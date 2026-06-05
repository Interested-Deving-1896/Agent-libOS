from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from enum import Enum
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models.exceptions import (
    CapabilityDenied,
    HumanApprovalRequired,
    NotFound,
    ProcessError,
    ProcessMessageWaitRequired,
    ProcessWaitRequired,
    ValidationError as LibOSValidationError,
)
from agent_libos.models import ToolSpec

InputT = TypeVar("InputT", bound=BaseModel)

_TOOL_DEFAULTS = DEFAULT_CONFIG.tools


class ToolErrorCode(str, Enum):
    VALIDATION_ERROR = "validation_error"
    PERMISSION_DENIED = "permission_denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    TRANSIENT_ERROR = "transient_error"
    EXECUTION_ERROR = "execution_error"
    UNSUPPORTED = "unsupported"


class ToolPolicy(BaseModel):
    side_effects: bool = False
    idempotent: bool = True
    requires_confirmation: bool = False
    permissions: set[str] = Field(default_factory=set)
    timeout_s: float | None = _TOOL_DEFAULTS.default_timeout_s
    max_retries: int = 0


class ToolContext(BaseModel):
    trace_id: str
    call_id: str
    pid: str
    workspace_id: str | None = None
    runtime: Any | None = Field(default=None, exclude=True)
    granted_permissions: set[str] = Field(default_factory=set)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)


class ToolArtifact(BaseModel):
    kind: str
    uri: str
    name: str | None = None
    mime_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolError(BaseModel):
    code: ToolErrorCode
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    ok: bool
    content: str = ""
    data: Any | None = None
    artifacts: list[ToolArtifact] = Field(default_factory=list)
    error: ToolError | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def success(
        cls,
        *,
        content: str = "",
        data: Any | None = None,
        artifacts: list[ToolArtifact] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(ok=True, content=content, data=data, artifacts=artifacts or [], metadata=metadata or {})

    @classmethod
    def failure(
        cls,
        *,
        code: ToolErrorCode,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            ok=False,
            content=message,
            error=ToolError(code=code, message=message, retryable=retryable, details=details or {}),
            metadata=metadata or {},
        )


class ToolExecutionError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: ToolErrorCode = ToolErrorCode.EXECUTION_ERROR,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class BaseAgentTool(ABC, Generic[InputT]):
    name: ClassVar[str]
    description: ClassVar[str]
    args_schema: ClassVar[type[InputT]]

    output_schema: ClassVar[type[BaseModel] | None] = None
    version: ClassVar[str] = _TOOL_DEFAULTS.version
    policy: ClassVar[ToolPolicy] = ToolPolicy()
    tags: ClassVar[list[str]] = []
    metadata: ClassVar[dict[str, Any]] = {}
    expose_internal_errors: ClassVar[bool] = False

    def spec(self) -> ToolSpec:
        self._validate_contract()
        policy = self.policy.model_dump()
        return ToolSpec(
            name=self.name,
            description=self.description,
            version=self.version,
            input_schema=self.args_schema.model_json_schema(),
            output_schema=self.output_schema.model_json_schema() if self.output_schema is not None else {},
            policy=policy,
            tags=list(self.tags),
            metadata=dict(self.metadata),
            required_capabilities=[
                {"resource": f"permission:{permission}", "rights": ["execute"]}
                for permission in sorted(self.policy.permissions)
            ],
            side_effects=sorted(self.policy.permissions) if self.policy.side_effects else [],
        )

    def to_openai_chat_tool(self) -> dict[str, Any]:
        spec = self.spec()
        return {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema,
            },
        }

    def to_mcp_tool(self) -> dict[str, Any]:
        spec = self.spec()
        return {
            "name": spec.name,
            "description": spec.description,
            "inputSchema": spec.input_schema,
            "_meta": {
                "version": spec.version,
                "tags": spec.tags,
                "policy": spec.policy,
            },
        }

    async def ainvoke(self, raw_args: Mapping[str, Any] | str | InputT, ctx: ToolContext) -> ToolResult:
        started_at = time.perf_counter()
        try:
            args = self.parse_args(raw_args)
        except PydanticValidationError as exc:
            return ToolResult.failure(
                code=ToolErrorCode.VALIDATION_ERROR,
                message=f"Invalid arguments for tool `{self.name}`.",
                details={"errors": exc.errors()},
                metadata=self._base_metadata(ctx, started_at),
            )
        except Exception as exc:
            return ToolResult.failure(
                code=ToolErrorCode.VALIDATION_ERROR,
                message=f"Failed to parse arguments for tool `{self.name}`.",
                details={"error_type": type(exc).__name__},
                metadata=self._base_metadata(ctx, started_at),
            )

        try:
            self._check_policy(ctx)
            if self.policy.timeout_s is None:
                raw_result = await self.execute(args, ctx)
            else:
                raw_result = await asyncio.wait_for(self.execute(args, ctx), timeout=self.policy.timeout_s)
            result = self._normalize_result(raw_result)
            result.metadata.update(self._base_metadata(ctx, started_at))
            return result
        except asyncio.TimeoutError:
            return ToolResult.failure(
                code=ToolErrorCode.TIMEOUT,
                message=f"Tool `{self.name}` timed out.",
                retryable=True,
                metadata=self._base_metadata(ctx, started_at),
            )
        except ToolExecutionError as exc:
            return ToolResult.failure(
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
                details=exc.details,
                metadata=self._base_metadata(ctx, started_at),
            )
        except HumanApprovalRequired:
            raise
        except ProcessWaitRequired:
            raise
        except ProcessMessageWaitRequired:
            raise
        except CapabilityDenied as exc:
            return ToolResult.failure(
                code=ToolErrorCode.PERMISSION_DENIED,
                message=str(exc),
                details={"error_type": type(exc).__name__},
                metadata=self._base_metadata(ctx, started_at),
            )
        except NotFound as exc:
            return ToolResult.failure(
                code=ToolErrorCode.EXECUTION_ERROR,
                message=str(exc),
                details={"error_type": type(exc).__name__},
                metadata=self._base_metadata(ctx, started_at),
            )
        except ProcessError as exc:
            return ToolResult.failure(
                code=ToolErrorCode.EXECUTION_ERROR,
                message=str(exc),
                details={"error_type": type(exc).__name__},
                metadata=self._base_metadata(ctx, started_at),
            )
        except LibOSValidationError as exc:
            return ToolResult.failure(
                code=ToolErrorCode.VALIDATION_ERROR,
                message=str(exc),
                details={"error_type": type(exc).__name__},
                metadata=self._base_metadata(ctx, started_at),
            )
        except Exception as exc:
            details: dict[str, Any] = {"error_type": type(exc).__name__}
            if self.expose_internal_errors:
                details["message"] = str(exc)
            return ToolResult.failure(
                code=ToolErrorCode.EXECUTION_ERROR,
                message=f"Tool `{self.name}` failed during execution.",
                details=details,
                metadata=self._base_metadata(ctx, started_at),
            )

    def invoke(self, raw_args: Mapping[str, Any] | str | InputT, ctx: ToolContext) -> ToolResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.ainvoke(raw_args, ctx))
        raise RuntimeError("Cannot call invoke() inside a running event loop. Use `await ainvoke(...)`.")

    def parse_args(self, raw_args: Mapping[str, Any] | str | InputT) -> InputT:
        if isinstance(raw_args, self.args_schema):
            return raw_args
        if isinstance(raw_args, str):
            return self.args_schema.model_validate_json(raw_args)
        if isinstance(raw_args, Mapping):
            return self.args_schema.model_validate(dict(raw_args))
        raise TypeError(f"Tool arguments must be {self.args_schema.__name__}, dict, or JSON string.")

    def _check_policy(self, ctx: ToolContext) -> None:
        missing_permissions = self.policy.permissions - ctx.granted_permissions
        if missing_permissions:
            raise ToolExecutionError(
                f"Permission denied for tool `{self.name}`.",
                code=ToolErrorCode.PERMISSION_DENIED,
                details={"missing_permissions": sorted(missing_permissions)},
            )
        if self.policy.requires_confirmation and not ctx.metadata.get("confirmed", False):
            raise ToolExecutionError(
                f"Confirmation required before executing tool `{self.name}`.",
                code=ToolErrorCode.CONFIRMATION_REQUIRED,
            )

    def _normalize_result(self, raw_result: Any) -> ToolResult:
        if isinstance(raw_result, ToolResult):
            return raw_result
        if isinstance(raw_result, BaseModel):
            return ToolResult.success(content=raw_result.model_dump_json(), data=raw_result.model_dump())
        if isinstance(raw_result, (dict, list)):
            return ToolResult.success(content=json.dumps(raw_result, ensure_ascii=False, default=str), data=raw_result)
        if raw_result is None:
            return ToolResult.success()
        return ToolResult.success(content=str(raw_result), data=raw_result)

    def _validate_contract(self) -> None:
        if not getattr(self, "name", None):
            raise TypeError(f"{self.__class__.__name__} must define non-empty `name`.")
        if not getattr(self, "description", None):
            raise TypeError(f"{self.__class__.__name__} must define non-empty `description`.")
        if not getattr(self, "args_schema", None):
            raise TypeError(f"{self.__class__.__name__} must define `args_schema`.")
        if not issubclass(self.args_schema, BaseModel):
            raise TypeError("`args_schema` must be a Pydantic BaseModel subclass.")
        if self.output_schema is not None and not issubclass(self.output_schema, BaseModel):
            raise TypeError("`output_schema` must be a Pydantic BaseModel subclass.")

    def _base_metadata(self, ctx: ToolContext, started_at: float) -> dict[str, Any]:
        return {
            "tool_name": self.name,
            "tool_version": self.version,
            "trace_id": ctx.trace_id,
            "call_id": ctx.call_id,
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
        }

    @abstractmethod
    async def execute(self, args: InputT, ctx: ToolContext) -> Any:
        raise NotImplementedError


class SyncAgentTool(BaseAgentTool[InputT], ABC):
    async def execute(self, args: InputT, ctx: ToolContext) -> Any:
        return await asyncio.to_thread(self.run, args, ctx)

    @abstractmethod
    def run(self, args: InputT, ctx: ToolContext) -> Any:
        raise NotImplementedError

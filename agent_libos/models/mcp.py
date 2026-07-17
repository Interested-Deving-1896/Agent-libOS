from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.models.base import StrEnum


class McpCallStatus(StrEnum):
    OK = "ok"
    MCP_ERROR = "mcp_error"
    TRANSPORT_ERROR = "transport_error"
    INVALID_RESPONSE = "invalid_response"
    RESPONSE_TOO_LARGE = "response_too_large"


@dataclass(frozen=True)
class McpHeaderSpec:
    env: str
    prefix: str = ""
    suffix: str = ""


@dataclass(frozen=True)
class McpStdioTransportSpec:
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None


@dataclass(frozen=True)
class McpHttpTransportSpec:
    url: str
    headers: dict[str, McpHeaderSpec] = field(default_factory=dict)


@dataclass(frozen=True)
class McpToolSpec:
    tool_id: str
    mcp_name: str
    right: str
    rollback_class: str
    state_mutation: bool
    information_flow: bool
    rollback_status: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpServerSpec:
    schema_version: int
    server_id: str
    transport: str
    tools: list[McpToolSpec]
    timeout_s: float
    max_request_bytes: int
    max_response_bytes: int
    stdio: McpStdioTransportSpec | None = None
    http: McpHttpTransportSpec | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def tool_by_id(self, tool_id: str) -> McpToolSpec | None:
        return next((tool for tool in self.tools if tool.tool_id == tool_id), None)


@dataclass(frozen=True)
class McpProviderTool:
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpToolListResult:
    server_id: str
    tools: list[McpProviderTool]
    response_bytes: int
    duration_s: float


@dataclass(frozen=True)
class McpProviderCallResult:
    content: Any = None
    structured_content: Any = None
    is_error: bool = False
    error: str | None = None
    response_bytes: int = 0
    duration_s: float = 0.0
    too_large: bool = False
    error_type: str | None = None
    correlation_id: str | None = None
    list_request_bytes: int = 0
    list_response_bytes: int = 0
    call_request_bytes: int = 0
    call_response_bytes: int = 0
    call_started: bool = False


@dataclass(frozen=True)
class McpCallResult:
    server_id: str
    tool_id: str
    mcp_name: str
    status: McpCallStatus
    ok: bool
    result: Any = None
    error: dict[str, Any] | None = None
    response_bytes: int = 0
    duration_s: float = 0.0

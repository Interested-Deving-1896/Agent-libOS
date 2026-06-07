from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.models.base import StrEnum


class JsonRpcCallStatus(StrEnum):
    OK = "ok"
    JSONRPC_ERROR = "jsonrpc_error"
    HTTP_ERROR = "http_error"
    TRANSPORT_ERROR = "transport_error"
    INVALID_RESPONSE = "invalid_response"
    RESPONSE_TOO_LARGE = "response_too_large"


@dataclass(frozen=True)
class JsonRpcHeaderSpec:
    env: str
    prefix: str = ""
    suffix: str = ""


@dataclass(frozen=True)
class JsonRpcMethodSpec:
    method_id: str
    rpc_method: str
    right: str
    rollback_class: str
    state_mutation: bool
    information_flow: bool
    rollback_status: str | None = None
    params_schema: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JsonRpcEndpointSpec:
    schema_version: int
    endpoint_id: str
    url: str
    headers: dict[str, JsonRpcHeaderSpec]
    methods: list[JsonRpcMethodSpec]
    timeout_s: float
    max_request_bytes: int
    max_response_bytes: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def method_by_id(self, method_id: str) -> JsonRpcMethodSpec | None:
        return next((method for method in self.methods if method.method_id == method_id), None)


@dataclass(frozen=True)
class JsonRpcTransportResult:
    status_code: int | None
    body: bytes
    elapsed_s: float
    response_bytes: int
    too_large: bool = False
    error: str | None = None


@dataclass(frozen=True)
class JsonRpcCallResult:
    endpoint_id: str
    method_id: str
    rpc_method: str
    request_id: str
    status: JsonRpcCallStatus
    http_status: int | None
    ok: bool
    result: Any = None
    error: dict[str, Any] | None = None
    response_bytes: int = 0
    duration_s: float = 0.0

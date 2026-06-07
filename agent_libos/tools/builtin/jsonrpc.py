from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.tools.base import BaseAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy
from agent_libos.utils.serde import to_jsonable

_JSONRPC_DEFAULTS = DEFAULT_CONFIG.jsonrpc


class ListJsonRpcEndpointsArgs(BaseModel):
    text: str | None = Field(default=None, description="Optional endpoint search text.")
    limit: int | None = Field(default=None, ge=1, le=_JSONRPC_DEFAULTS.list_limit)


class ListJsonRpcEndpointsOutput(BaseModel):
    endpoints: list[dict[str, Any]]


class InspectJsonRpcEndpointArgs(BaseModel):
    endpoint_id: str


class InspectJsonRpcEndpointOutput(BaseModel):
    endpoint: dict[str, Any]


class CallJsonRpcMethodArgs(BaseModel):
    endpoint_id: str
    method_id: str
    params: Any = Field(default=None, description="JSON-RPC params object, array, or null.")


class CallJsonRpcMethodOutput(BaseModel):
    endpoint_id: str
    method_id: str
    rpc_method: str
    request_id: str
    status: str
    http_status: int | None
    ok: bool
    result: Any = None
    error: dict[str, Any] | None = None
    response_bytes: int
    duration_s: float


class ListJsonRpcEndpointsTool(BaseAgentTool[ListJsonRpcEndpointsArgs]):
    name = "list_jsonrpc_endpoints"
    description = "List registered JSON-RPC endpoint metadata through the libOS JSON-RPC primitive."
    args_schema = ListJsonRpcEndpointsArgs
    output_schema = ListJsonRpcEndpointsOutput
    policy = ToolPolicy(side_effects=False, idempotent=True, permissions={"jsonrpc_endpoint.read"})
    tags = ["jsonrpc", "remote"]

    async def execute(self, args: ListJsonRpcEndpointsArgs, ctx: ToolContext) -> ListJsonRpcEndpointsOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        endpoints = runtime.jsonrpc.list_endpoints(actor=ctx.pid, text=args.text, limit=args.limit)
        return ListJsonRpcEndpointsOutput(endpoints=endpoints)


class InspectJsonRpcEndpointTool(BaseAgentTool[InspectJsonRpcEndpointArgs]):
    name = "inspect_jsonrpc_endpoint"
    description = "Inspect one registered JSON-RPC endpoint without exposing resolved secrets."
    args_schema = InspectJsonRpcEndpointArgs
    output_schema = InspectJsonRpcEndpointOutput
    policy = ToolPolicy(side_effects=False, idempotent=True, permissions={"jsonrpc_endpoint.read"})
    tags = ["jsonrpc", "remote"]

    async def execute(self, args: InspectJsonRpcEndpointArgs, ctx: ToolContext) -> InspectJsonRpcEndpointOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        return InspectJsonRpcEndpointOutput(
            endpoint=runtime.jsonrpc.inspect_endpoint(args.endpoint_id, actor=ctx.pid)
        )


class CallJsonRpcMethodTool(BaseAgentTool[CallJsonRpcMethodArgs]):
    name = "call_jsonrpc_method"
    description = (
        "Call a pre-registered JSON-RPC over HTTP method. The primitive enforces endpoint registration, "
        "method capability, human approval, audit, and provider external-effect classification."
    )
    args_schema = CallJsonRpcMethodArgs
    output_schema = CallJsonRpcMethodOutput
    policy = ToolPolicy(side_effects=True, idempotent=False, permissions={"jsonrpc.call"}, timeout_s=None)
    tags = ["jsonrpc", "remote", "external"]

    async def execute(self, args: CallJsonRpcMethodArgs, ctx: ToolContext) -> CallJsonRpcMethodOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        result = await runtime.jsonrpc.acall(ctx.pid, args.endpoint_id, args.method_id, args.params)
        return CallJsonRpcMethodOutput(**to_jsonable(result))

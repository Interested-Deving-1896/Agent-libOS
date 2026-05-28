from __future__ import annotations

from pydantic import BaseModel, Field

from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy


class RequestPermissionArgs(BaseModel):
    resource: str = Field(description="Capability resource to request, such as filesystem:workspace:path.txt.")
    rights: list[str] = Field(description="Capability rights to request, such as ['write'].")
    reason: str = Field(description="Brief reason shown to the human.")
    human: str = Field(default="owner", description="Human recipient name.")


class RequestPermissionOutput(BaseModel):
    request_id: str
    resource: str
    rights: list[str]
    status: str


class RequestPermissionTool(SyncAgentTool[RequestPermissionArgs]):
    name = "request_permission"
    description = (
        "Ask the human to set a permission policy for a libOS capability resource. "
        "The human can always allow, always deny, or require per-use approval."
    )
    args_schema = RequestPermissionArgs
    output_schema = RequestPermissionOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=True, idempotent=False, timeout_s=5.0)
    tags = ["permission", "human", "capability"]

    def run(self, args: RequestPermissionArgs, ctx: ToolContext) -> RequestPermissionOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        if not args.rights:
            raise ToolExecutionError(
                "At least one capability right is required.",
                code=ToolErrorCode.VALIDATION_ERROR,
            )
        request_id = runtime.human.request_permission(
            pid=ctx.pid,
            human=args.human,
            resource=args.resource,
            rights=args.rights,
            reason=args.reason,
        )
        return RequestPermissionOutput(
            request_id=request_id,
            resource=args.resource,
            rights=args.rights,
            status="pending",
        )

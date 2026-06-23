from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent_libos.models import CapabilityEffect
from agent_libos.models.exceptions import CapabilityDenied
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy


class ListCapabilitiesArgs(BaseModel):
    include_inactive: bool = Field(default=False, description="Include revoked, disabled, or expired capabilities.")


class ListCapabilitiesOutput(BaseModel):
    capabilities: list[dict[str, Any]]


class InspectCapabilityArgs(BaseModel):
    cap_id: str


class InspectCapabilityOutput(BaseModel):
    capability: dict[str, Any]


class DelegateCapabilityArgs(BaseModel):
    child_pid: str
    resource: str
    rights: list[str]
    effect: str = Field(default=CapabilityEffect.ALLOW.value)
    expires_at: str | None = None
    uses_remaining: int | None = None
    delegable: bool = False
    constraints: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DelegateCapabilityOutput(BaseModel):
    capability: dict[str, Any]


class RevokeCapabilityArgs(BaseModel):
    cap_id: str
    reason: str | None = None


class RevokeCapabilityOutput(BaseModel):
    capability: dict[str, Any]


def _runtime(ctx: ToolContext) -> Any:
    if ctx.runtime is None:
        raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
    return ctx.runtime


class ListCapabilitiesTool(SyncAgentTool[ListCapabilitiesArgs]):
    name = "list_capabilities"
    description = "List the current process capabilities without granting new authority."
    args_schema = ListCapabilitiesArgs
    output_schema = ListCapabilitiesOutput
    policy = ToolPolicy(side_effects=False)
    tags = ["capability", "authority"]

    def run(self, args: ListCapabilitiesArgs, ctx: ToolContext) -> ListCapabilitiesOutput:
        runtime = _runtime(ctx)
        caps = runtime.capability.list_subject(ctx.pid, include_inactive=args.include_inactive)
        return ListCapabilitiesOutput(capabilities=[runtime.capability.inspect(cap.cap_id) for cap in caps])


class InspectCapabilityTool(SyncAgentTool[InspectCapabilityArgs]):
    name = "inspect_capability"
    description = "Inspect one capability owned by the current process."
    args_schema = InspectCapabilityArgs
    output_schema = InspectCapabilityOutput
    policy = ToolPolicy(side_effects=False)
    tags = ["capability", "authority"]

    def run(self, args: InspectCapabilityArgs, ctx: ToolContext) -> InspectCapabilityOutput:
        runtime = _runtime(ctx)
        cap = runtime.store.get_capability(args.cap_id)
        if cap is None:
            raise ToolExecutionError("Capability not found.", code=ToolErrorCode.VALIDATION_ERROR)
        if cap.subject != ctx.pid:
            raise ToolExecutionError("Cannot inspect another process capability.", code=ToolErrorCode.PERMISSION_DENIED)
        return InspectCapabilityOutput(capability=runtime.capability.inspect(args.cap_id))


class DelegateCapabilityTool(SyncAgentTool[DelegateCapabilityArgs]):
    name = "delegate_capability"
    description = "Delegate an attenuated capability to a direct child process."
    args_schema = DelegateCapabilityArgs
    output_schema = DelegateCapabilityOutput
    policy = ToolPolicy(side_effects=True, idempotent=False, declared_permissions={"capability.write"})
    tags = ["capability", "authority"]

    def run(self, args: DelegateCapabilityArgs, ctx: ToolContext) -> DelegateCapabilityOutput:
        runtime = _runtime(ctx)
        child = runtime.process.get(args.child_pid)
        if child.parent_pid != ctx.pid:
            raise ToolExecutionError("Capabilities can only be delegated to a direct child.", code=ToolErrorCode.PERMISSION_DENIED)
        try:
            cap = runtime.capability.delegate(
                ctx.pid,
                args.child_pid,
                {
                    "resource": args.resource,
                    "rights": args.rights,
                    "effect": args.effect,
                    "expires_at": args.expires_at,
                    "uses_remaining": args.uses_remaining,
                    "delegable": args.delegable,
                    "constraints": args.constraints,
                    "metadata": args.metadata,
                },
                actor=ctx.pid,
            )
        except CapabilityDenied as exc:
            raise ToolExecutionError(str(exc), code=ToolErrorCode.PERMISSION_DENIED) from exc
        return DelegateCapabilityOutput(capability=runtime.capability.inspect(cap.cap_id))


class RevokeCapabilityTool(SyncAgentTool[RevokeCapabilityArgs]):
    name = "revoke_capability"
    description = "Revoke a capability when the current process has holder, issuer, revoke, or admin authority."
    args_schema = RevokeCapabilityArgs
    output_schema = RevokeCapabilityOutput
    policy = ToolPolicy(side_effects=True, idempotent=False, declared_permissions={"capability.write"})
    tags = ["capability", "authority"]

    def run(self, args: RevokeCapabilityArgs, ctx: ToolContext) -> RevokeCapabilityOutput:
        runtime = _runtime(ctx)
        try:
            cap = runtime.capability.revoke(args.cap_id, revoked_by=ctx.pid, reason=args.reason)
        except CapabilityDenied as exc:
            raise ToolExecutionError(str(exc), code=ToolErrorCode.PERMISSION_DENIED) from exc
        return RevokeCapabilityOutput(capability=runtime.capability.inspect(cap.cap_id))

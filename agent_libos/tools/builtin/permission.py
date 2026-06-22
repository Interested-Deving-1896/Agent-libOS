from __future__ import annotations

import json
import threading

from pydantic import BaseModel, Field

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models import CapabilityRight, HumanRequestStatus
from agent_libos.models.exceptions import HumanResponseRequired
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy

_RUNTIME_DEFAULTS = DEFAULT_CONFIG.runtime
_TOOL_DEFAULTS = DEFAULT_CONFIG.tools


class RequestPermissionArgs(BaseModel):
    resource: str = Field(description="Capability resource to request, such as filesystem:workspace:path.txt.")
    rights: list[str] = Field(description="Capability rights to request, such as ['write'].")
    reason: str = Field(description="Brief reason shown to the human.")
    human: str = Field(default=_RUNTIME_DEFAULTS.default_human, description="Human recipient name.")


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
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        declared_permissions={"human.ask", "capability.write"},
        timeout_s=_TOOL_DEFAULTS.standard_timeout_s,
    )
    tags = ["permission", "human", "capability"]

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending_by_key: dict[str, str] = {}

    def run(self, args: RequestPermissionArgs, ctx: ToolContext) -> RequestPermissionOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        if not args.rights:
            raise ToolExecutionError(
                "At least one capability right is required.",
                code=ToolErrorCode.VALIDATION_ERROR,
            )
        try:
            rights = [CapabilityRight(str(right)).value for right in args.rights]
        except ValueError as exc:
            raise ToolExecutionError(
                f"Unknown capability right: {exc}",
                code=ToolErrorCode.VALIDATION_ERROR,
            ) from exc
        key = self._pending_key(ctx.pid, args, rights)
        request_id = ctx.metadata.get("human_resume_request_id")
        if not isinstance(request_id, str) or not request_id:
            with self._lock:
                request_id = self._pending_by_key.get(key)
        if request_id is None:
            request_id = runtime.human.request_permission(
                pid=ctx.pid,
                human=args.human,
                resource=args.resource,
                rights=rights,
                reason=args.reason,
            )
            with self._lock:
                self._pending_by_key[key] = request_id
            raise HumanResponseRequired(
                request_id=request_id,
                message=f"{ctx.pid} is waiting for human permission decision {request_id}",
            )
        request = runtime.human.get(request_id)
        if request.status == HumanRequestStatus.PENDING:
            raise HumanResponseRequired(
                request_id=request_id,
                message=f"{ctx.pid} is waiting for human permission decision {request_id}",
            )
        with self._lock:
            self._pending_by_key.pop(key, None)
        if request.status not in {HumanRequestStatus.APPROVED, HumanRequestStatus.REJECTED}:
            raise ToolExecutionError(
                f"Human permission request {request_id} ended with status={request.status.value}.",
                code=ToolErrorCode.EXECUTION_ERROR,
            )
        return RequestPermissionOutput(
            request_id=request_id,
            resource=args.resource,
            rights=rights,
            status=request.status.value,
        )

    def _pending_key(self, pid: str, args: RequestPermissionArgs, rights: list[str]) -> str:
        payload = {
            "pid": pid,
            "human": args.human,
            "resource": args.resource,
            "rights": rights,
            "reason": args.reason,
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)

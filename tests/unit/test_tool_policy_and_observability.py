from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolPolicy
from agent_libos.tools.observability import sanitize_for_observability


class EmptyArgs(BaseModel):
    pass


class MetadataOnlyTool(SyncAgentTool[EmptyArgs]):
    name = "metadata_only_tool"
    description = "Exercise ToolPolicy metadata semantics."
    args_schema = EmptyArgs
    policy = ToolPolicy(
        side_effects=True,
        idempotent=False,
        declared_confirmation_required=True,
        declared_permissions={"filesystem.write"},
    )

    def run(self, args: EmptyArgs, ctx: ToolContext) -> dict[str, Any]:
        return {"ok": True, "pid": ctx.pid}


class TestToolPolicyAndObservability:
    def test_tool_policy_is_metadata_not_self_granted_authority(self) -> None:
        tool = MetadataOnlyTool()
        result = tool.invoke(
            {},
            ToolContext(trace_id="trace", call_id="call", pid="pid_test", metadata={}),
        )
        spec = tool.spec()

        assert result.ok
        assert result.data == {"ok": True, "pid": "pid_test"}
        assert spec.required_capabilities == []
        assert spec.policy["declared_permissions"] == {"filesystem.write"}
        assert spec.policy["declared_confirmation_required"] is True

    def test_observability_sanitizes_sensitive_fields_with_stable_hash(self) -> None:
        secret = "SECRET_TOKEN_SHOULD_NOT_APPEAR"
        value = {
            "path": "notes.txt",
            "content": secret,
            "payload": {"nested": secret},
            "metadata": {"source_code": secret, "tests": [{"body": secret}]},
        }

        first = sanitize_for_observability(value)
        second = sanitize_for_observability(value)

        assert first["sha256"] == second["sha256"]
        assert first["bytes"] == second["bytes"]
        assert secret not in first["preview"]
        assert first["redacted"] is True
        assert "sha256" in first["preview"]

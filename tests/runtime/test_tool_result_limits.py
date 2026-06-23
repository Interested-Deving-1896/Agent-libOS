from __future__ import annotations

import json
import tempfile
import time

from pydantic import BaseModel

from agent_libos import Runtime
from agent_libos.models import ObjectType
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolPolicy


class EmptyArgs(BaseModel):
    pass


class HugeResultTool(SyncAgentTool[EmptyArgs]):
    name = "huge_result"
    description = "Return a result larger than the broker persistence boundary."
    args_schema = EmptyArgs

    def run(self, args: EmptyArgs, ctx: ToolContext) -> dict[str, str]:
        assert ctx.runtime is not None
        return {"blob": "x" * (ctx.runtime.config.tools.tool_result_payload_hard_limit_bytes + 1)}


class HugeSideEffectResultTool(SyncAgentTool[EmptyArgs]):
    name = "huge_side_effect_result"
    description = "Mutate runtime state and then return a result larger than the broker persistence boundary."
    args_schema = EmptyArgs
    policy = ToolPolicy(side_effects=True, idempotent=False, declared_permissions={"object.write"})

    def run(self, args: EmptyArgs, ctx: ToolContext) -> dict[str, str]:
        assert ctx.runtime is not None
        ctx.runtime.memory.create_object(
            ctx.pid,
            ObjectType.OBSERVATION,
            {"committed": True},
            name="huge.side.effect.committed",
        )
        return {"blob": "x" * (ctx.runtime.config.tools.tool_result_payload_hard_limit_bytes + 1)}


class SlowSyncSideEffectTool(SyncAgentTool[EmptyArgs]):
    name = "slow_sync_side_effect"
    description = "Sleep past the declared timeout and then mutate runtime state."
    args_schema = EmptyArgs
    policy = ToolPolicy(side_effects=True, timeout_s=0.01)

    def run(self, args: EmptyArgs, ctx: ToolContext) -> dict[str, bool]:
        time.sleep(0.05)
        assert ctx.runtime is not None
        ctx.runtime.memory.create_object(
            ctx.pid,
            ObjectType.OBSERVATION,
            {"late": True},
            name="sync.side.effect.done",
        )
        return {"done": True}


class ExpectedOutput(BaseModel):
    value: int


class BadStaticOutputTool(SyncAgentTool[EmptyArgs]):
    name = "bad_static_output"
    description = "Return data that violates output_schema."
    args_schema = EmptyArgs
    output_schema = ExpectedOutput

    def run(self, args: EmptyArgs, ctx: ToolContext) -> dict[str, str]:
        return {"value": "not an int"}


class StaticToolV1(SyncAgentTool[EmptyArgs]):
    name = "same_static_tool"
    description = "v1 description"
    args_schema = EmptyArgs

    def run(self, args: EmptyArgs, ctx: ToolContext) -> dict[str, int]:
        return {"version": 1}


class StaticToolV2(SyncAgentTool[EmptyArgs]):
    name = "same_static_tool"
    description = "v2 description"
    args_schema = EmptyArgs

    def run(self, args: EmptyArgs, ctx: ToolContext) -> dict[str, int]:
        return {"version": 2}


class TestToolResultLimits:
    def test_tool_result_payload_limit_rejects_before_result_object_creation(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="huge result")
            handle = runtime.tools.register_tool(HugeResultTool(), registered_by="test", ephemeral=True)
            runtime.tools.configure_process_tools(pid, [handle], assigned_by="test")

            result = runtime.tools.call(pid, "huge_result", {})

            assert not result.ok
            assert result.result_handle is None
            assert "tool result payload exceeds" in (result.error or "")
            assert [obj for obj in runtime.store.list_objects() if obj.type.value == "tool_result"] == []
            audit = [record for record in runtime.audit.trace() if record.action == "tool.call"][-1]
            assert audit.decision["result"]["preview"] == "[tool result omitted after size-limit failure]"
        finally:
            runtime.close()

    def test_side_effect_tool_oversize_result_is_reported_as_omitted_success(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="huge side effect result")
            handle = runtime.tools.register_tool(HugeSideEffectResultTool(), registered_by="test", ephemeral=True)
            runtime.tools.configure_process_tools(pid, [handle], assigned_by="test")

            result = runtime.tools.call(pid, "huge_side_effect_result", {})

            assert result.ok, result.error
            assert result.result_handle is not None
            assert result.payload["result_omitted"] is True
            assert runtime.store.get_object_by_name(
                "huge.side.effect.committed",
                namespace=runtime.memory.resolve_namespace(pid),
            ) is not None
            stored = runtime.store.get_object(result.result_handle.oid)
            assert stored is not None
            assert stored.payload["metadata"]["result_omitted"] is True
            audit = [record for record in runtime.audit.trace() if record.action == "tool.call"][-1]
            assert audit.decision["ok"] is True
            assert audit.decision["result_omitted"] is True
        finally:
            runtime.close()

    def test_sync_side_effect_tool_does_not_return_before_background_work_finishes(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="sync timeout")
            handle = runtime.tools.register_tool(SlowSyncSideEffectTool(), registered_by="test", ephemeral=True)
            runtime.tools.configure_process_tools(pid, [handle], assigned_by="test")

            result = runtime.tools.call(pid, "slow_sync_side_effect", {})

            assert result.ok, result.error
            assert runtime.store.get_object_by_name(
                "sync.side.effect.done",
                namespace=runtime.memory.resolve_namespace(pid),
            ) is not None
        finally:
            runtime.close()

    def test_static_tool_output_schema_is_enforced(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="bad output")
            handle = runtime.tools.register_tool(BadStaticOutputTool(), registered_by="test", ephemeral=True)
            runtime.tools.configure_process_tools(pid, [handle], assigned_by="test")

            result = runtime.tools.call(pid, "bad_static_output", {})

            assert not result.ok
            assert "Invalid output" in (result.error or "")
            assert result.result_handle is None
        finally:
            runtime.close()

    def test_static_tool_spec_is_refreshed_when_runtime_reopens_with_new_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = f"{temp_dir}/runtime.sqlite"
            runtime = Runtime.open(db_path)
            try:
                runtime.tools.register_tool(StaticToolV1(), registered_by="test")
            finally:
                runtime.close()

            runtime = Runtime.open(db_path)
            try:
                runtime.tools.register_tool(StaticToolV2(), registered_by="test")
                row = next(row for row in runtime.store.list_tools() if row["name"] == "same_static_tool")
                spec = json.loads(row["spec_json"])
                assert spec["description"] == "v2 description"
            finally:
                runtime.close()

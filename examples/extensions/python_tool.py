#!/usr/bin/env python3
"""Run a Python tool through ToolBroker, including a primitive denial path."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_libos import Runtime
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models import CapabilityRight
from agent_libos.substrate import LocalResourceProviderSubstrate
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolPolicy


class ReadSizeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, description="File path relative to the process cwd.")


class ReadSizeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    bytes_read: int
    truncated: bool


class ReadSizeTool(SyncAgentTool[ReadSizeArgs]):
    name = "example_read_size"
    description = "Read a bounded UTF-8 file prefix and report its actual byte count."
    args_schema = ReadSizeArgs
    output_schema = ReadSizeResult
    # This declaration describes the tool; it never grants filesystem access.
    policy = ToolPolicy(side_effects=False, idempotent=True)

    def run(self, args: ReadSizeArgs, ctx: ToolContext) -> ReadSizeResult:
        if ctx.runtime is None:
            raise RuntimeError("a Runtime tool context is required")
        result = ctx.runtime.filesystem.read_text(
            ctx.pid,
            args.path,
            cwd=ctx.runtime.process.working_directory(ctx.pid),
            max_bytes=DEFAULT_CONFIG.tools.filesystem_read_max_bytes,
        )
        return ReadSizeResult(bytes_read=result.bytes_read, truncated=result.truncated)


def run_example() -> dict[str, Any]:
    # Host fixture setup occurs before process execution, outside the tool.
    with TemporaryDirectory(prefix="agent-libos-python-tool-") as directory:
        workspace = Path(directory)
        (workspace / "note.txt").write_text("中文", encoding="utf-8")
        runtime = Runtime.open(
            ":memory:",
            config=DEFAULT_CONFIG,
            substrate=LocalResourceProviderSubstrate(workspace),
        )
        try:
            pid = runtime.process.spawn(goal="Read the example note's byte count")
            handle = runtime.tools.register_tool(ReadSizeTool(), registered_by="example.host")
            # This Host method replaces both process tables. Only this demo tool is needed.
            runtime.tools.configure_process_tools(pid, [handle], assigned_by="example.host")

            denied = runtime.tools.call(pid, handle, {"path": "note.txt"})
            assert not denied.ok
            assert denied.error is not None
            assert "CapabilityDenied" in denied.error
            assert not runtime.store.list_external_effects(pid=pid)

            # Host composition issues one narrow read; model code cannot mint this grant.
            runtime.capability.issue_trusted(
                pid,
                "filesystem:workspace:note.txt",
                [CapabilityRight.READ],
                issued_by="example.host",
                uses_remaining=1,
            )
            result = runtime.tools.call(pid, handle, {"path": "note.txt"})
            assert result.ok, result.error
            assert result.payload == {"bytes_read": 6, "truncated": False}
            return {
                "denied_before_grant": True,
                "tool_visible": handle.name in runtime.tools.model_tool_names(pid),
                "result": result.payload,
            }
        finally:
            closed = runtime.close()
            assert closed["ok"], closed


if __name__ == "__main__":
    print(json.dumps(run_example(), sort_keys=True))

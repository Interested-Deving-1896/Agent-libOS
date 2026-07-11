from __future__ import annotations

from pathlib import Path

from agent_libos import Runtime
from agent_libos.utils.serde import dumps


def test_review_image_projects_small_model_schema_without_removing_callable_tools(tmp_path: Path) -> None:
    runtime = Runtime.open(tmp_path / "projection.sqlite")
    try:
        pid = runtime.process.spawn(image="review-agent:v0", goal="tool projection")
        process = runtime.process.get(pid)
        initial_schema = runtime.tools.openai_tool_schemas(pid)

        assert len(process.tool_table) > len(process.model_tool_table)
        assert len(process.model_tool_table) == 10
        assert "read_text_file" in process.tool_table
        assert "read_text_file" not in process.model_tool_table
        assert len(dumps(initial_schema).encode("utf-8")) < 20_000

        capabilities_before = {item.cap_id for item in runtime.store.list_capabilities(subject=pid)}
        activated = runtime.tools.activate_tool_group(pid, "filesystem")
        capabilities_after = {item.cap_id for item in runtime.store.list_capabilities(subject=pid)}

        assert activated["authority_changed"] is False
        assert activated["schema_bytes_after"] > activated["schema_bytes_before"]
        assert capabilities_after == capabilities_before
        assert "read_text_file" in runtime.process.get(pid).model_tool_table
    finally:
        runtime.close()


def test_model_tool_projection_survives_runtime_reopen(tmp_path: Path) -> None:
    database = tmp_path / "projection-reopen.sqlite"
    runtime = Runtime.open(database)
    try:
        pid = runtime.process.spawn(image="review-agent:v0", goal="persist projection")
        runtime.tools.activate_tool_group(pid, "remote")
        expected = dict(runtime.process.get(pid).model_tool_table)
    finally:
        runtime.close()

    reopened = Runtime.open(database)
    try:
        assert reopened.process.get(pid).model_tool_table == expected
        assert reopened.tools.model_tool_table(pid) == expected
    finally:
        reopened.close()

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from agent_libos.config import AgentLibOSConfig, RuntimeDefaults
from agent_libos.models import (
    CapabilityRight,
    JsonRpcEndpointSpec,
    JsonRpcMethodSpec,
    LLMCallRecord,
    McpServerSpec,
    McpStdioTransportSpec,
    McpToolSpec,
    ObjectType,
)
from agent_libos.runtime.runtime import Runtime
from agent_libos.utils.ids import utc_now


STORE_BACKENDS = [
    "sqlite-memory",
    "sqlite-file",
    pytest.param("postgres", marks=pytest.mark.postgres),
]

PERSISTENT_STORE_BACKENDS = [
    "sqlite-file",
    pytest.param("postgres", marks=pytest.mark.postgres),
]


@contextlib.contextmanager
def _runtime_for_backend(kind: str, tmp_path: Path) -> Iterator[Runtime]:
    target: str | Path | None
    config = AgentLibOSConfig()
    postgres_context = contextlib.nullcontext(None)
    if kind == "sqlite-memory":
        target = "local"
    elif kind == "sqlite-file":
        target = tmp_path / "contract.sqlite"
    elif kind == "postgres":
        postgres_context = _postgres_schema_dsn()
        target = None
    else:
        raise AssertionError(f"unknown backend: {kind}")

    with postgres_context as dsn:
        if dsn is not None:
            config = AgentLibOSConfig(runtime=RuntimeDefaults(store_backend="postgres", store_dsn=dsn))
            target = dsn
        runtime = Runtime.open(target, config=config)
        try:
            yield runtime
        finally:
            runtime.close()


@contextlib.contextmanager
def _persistent_target(kind: str, tmp_path: Path) -> Iterator[tuple[str | Path | None, AgentLibOSConfig]]:
    if kind == "sqlite-file":
        yield tmp_path / "payload.sqlite", AgentLibOSConfig()
        return
    if kind == "postgres":
        with _postgres_schema_dsn() as dsn:
            yield dsn, AgentLibOSConfig(runtime=RuntimeDefaults(store_backend="postgres", store_dsn=dsn))
        return
    raise AssertionError(f"unknown persistent backend: {kind}")


@contextlib.contextmanager
def _postgres_schema_dsn() -> Iterator[str]:
    dsn = os.environ["AGENT_LIBOS_POSTGRES_DSN"]
    schema = f"agent_libos_contract_{uuid4().hex}"
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield _dsn_with_search_path(dsn, schema)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _dsn_with_search_path(dsn: str, schema: str) -> str:
    parsed = urlsplit(dsn)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


@pytest.mark.parametrize("kind", STORE_BACKENDS)
def test_runtime_store_contract_core_records(kind: str, tmp_path: Path) -> None:
    with _runtime_for_backend(kind, tmp_path) as runtime:
        pid = runtime.process.spawn(goal=f"{kind} store contract")
        handle = runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.ARTIFACT,
            payload={"backend": kind, "items": [1, 2, 3]},
            name="contract.payload",
        )
        runtime.capability.grant(pid, "filesystem:workspace:*", [CapabilityRight.READ], issued_by="test")
        runtime.capability.grant(pid, "human:owner", [CapabilityRight.WRITE], issued_by="test")
        runtime.messages.post(sender="human:owner", recipient_pid=pid, subject="contract", body="hello")
        runtime.human.output(pid, "visible status", channel="gui")
        runtime.ratings.upsert(pid, score=5, comment="works")

        endpoint = JsonRpcEndpointSpec(
            schema_version=1,
            endpoint_id="contract-jsonrpc",
            url="https://api.example.test/jsonrpc",
            headers={},
            methods=[
                JsonRpcMethodSpec(
                    method_id="echo",
                    rpc_method="demo.echo",
                    right="read",
                    rollback_class="no_rollback_required",
                    state_mutation=False,
                    information_flow=True,
                )
            ],
            timeout_s=1.0,
            max_request_bytes=1024,
            max_response_bytes=2048,
        )
        runtime.store.upsert_jsonrpc_endpoint(endpoint, registered_by="test", created_at=utc_now())
        mcp_server = McpServerSpec(
            schema_version=1,
            server_id="contract-mcp",
            transport="stdio",
            stdio=McpStdioTransportSpec(command="python3", args=["-m", "demo_mcp"], env={}),
            tools=[
                McpToolSpec(
                    tool_id="echo",
                    mcp_name="demo.echo",
                    right="read",
                    rollback_class="no_rollback_required",
                    state_mutation=False,
                    information_flow=True,
                )
            ],
            timeout_s=1.0,
            max_request_bytes=1024,
            max_response_bytes=2048,
        )
        runtime.store.upsert_mcp_server(mcp_server, registered_by="test", created_at=utc_now())
        runtime.store.insert_llm_call(
            LLMCallRecord(
                call_id="contract-llm-call",
                pid=pid,
                image_id="base-agent:v0",
                purpose="contract",
                status="ok",
                messages=[{"role": "user", "content": kind}],
                response_content="ok",
                created_at=utc_now(),
            )
        )
        checkpoint_id = runtime.checkpoint.create(pid, "store contract", require_capability=False)

        assert runtime.process.get(pid) is not None
        assert runtime.memory.get_object(pid, handle).payload["backend"] == kind
        assert runtime.store.list_capabilities(subject=pid)
        assert runtime.audit.trace()
        assert runtime.events.list(target=pid)
        assert runtime.human.list(pid=pid)
        assert runtime.messages.list(pid)
        assert runtime.ratings.get(pid).score == 5
        assert runtime.store.get_jsonrpc_endpoint("contract-jsonrpc")[0].method_by_id("echo") is not None
        assert runtime.store.get_mcp_server("contract-mcp")[0].tool_by_id("echo") is not None
        assert runtime.store.list_llm_calls(pid=pid)[0].call_id == "contract-llm-call"
        assert runtime.store.get_checkpoint_snapshot(checkpoint_id) is not None


@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_object_payload_is_runtime_only_across_reopen(kind: str, tmp_path: Path) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        runtime = Runtime.open(target, config=config)
        try:
            pid = runtime.process.spawn(goal=f"{kind} payload reopen")
            handle = runtime.memory.create_object(
                pid=pid,
                object_type=ObjectType.ARTIFACT,
                payload={"secret": "runtime-only", "backend": kind},
                name="runtime.only.payload",
            )
            assert runtime.memory.get_object(pid, handle).payload == {"secret": "runtime-only", "backend": kind}
            row = runtime.store.select_table_rows("objects", "oid = ?", (handle.oid,))[0]
            assert json.loads(row["payload_json"]) == {"storage": "runtime_memory", "present": True}
        finally:
            runtime.close()

        reopened = Runtime.open(target, config=config)
        try:
            obj = reopened.store.get_object(handle.oid)
            assert obj is None
            row = reopened.store.select_table_rows("objects", "oid = ?", (handle.oid,))[0]
            assert row["lifecycle_state"] == "released"
            assert json.loads(row["payload_json"]) == {"storage": "runtime_memory", "present": False}
        finally:
            reopened.close()

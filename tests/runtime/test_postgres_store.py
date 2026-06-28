from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from agent_libos.config import AgentLibOSConfig, RuntimeDefaults
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import (
    CapabilityRight,
    JsonRpcEndpointSpec,
    JsonRpcMethodSpec,
    LLMCallRecord,
)
from agent_libos.runtime.runtime import Runtime
from agent_libos.storage import open_store
from agent_libos.utils.ids import utc_now


class _ScriptedActionClient:
    def __init__(self) -> None:
        self.actions = [{"action": "get_current_time", "timezone": "UTC"}]

    def complete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        action = self.actions.pop(0)
        name = str(action["action"])
        args = {key: value for key, value in action.items() if key != "action"}
        return LLMCompletion(content="", tool_calls=[{"id": "pg_call", "name": name, "arguments": json.dumps(args)}])


@contextlib.contextmanager
def _postgres_schema_dsn() -> Iterator[str]:
    dsn = os.environ["AGENT_LIBOS_POSTGRES_DSN"]
    schema = f"agent_libos_test_{uuid4().hex}"
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


@pytest.mark.postgres
class TestPostgresStore:
    def test_postgres_runtime_store_smoke(self) -> None:
        with _postgres_schema_dsn() as dsn:
            config = AgentLibOSConfig(runtime=RuntimeDefaults(store_backend="postgres", store_dsn=dsn))
            store = open_store(dsn, config=config)
            runtime = Runtime(store, config=config, llm_client=_ScriptedActionClient())
            try:
                pid = runtime.process.spawn(goal="postgres store smoke")
                runtime.capability.grant(pid, "filesystem:workspace:*", [CapabilityRight.READ], issued_by="test")
                runtime.messages.post(sender="human:owner", recipient_pid=pid, subject="hello", body="postgres")

                endpoint = JsonRpcEndpointSpec(
                    schema_version=1,
                    endpoint_id="pg-demo",
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
                assert runtime.store.get_jsonrpc_endpoint("pg-demo")[0].method_by_id("echo") is not None

                runtime.store.insert_llm_call(
                    LLMCallRecord(
                        call_id="llm_pg_smoke",
                        pid=pid,
                        image_id="base-agent:v0",
                        purpose="test",
                        status="ok",
                        messages=[{"role": "user", "content": "postgres"}],
                        response_content="ok",
                        created_at=utc_now(),
                    )
                )
                assert runtime.store.list_llm_calls(pid=pid)[0].call_id == "llm_pg_smoke"

                result = runtime.run_process_until_idle(pid, max_quanta=1)
                assert result[0]["action"]["action"] == "get_current_time"

                checkpoint_id = runtime.checkpoint.create(pid, "postgres smoke", require_capability=False)
                restored = runtime.checkpoint.restore("test", checkpoint_id, require_capability=False)
                forked = runtime.checkpoint.fork_from_checkpoint("test", checkpoint_id, require_capability=False)

                assert restored["status"] == "restored"
                assert runtime.process.get(forked["fork_root_pid"]) is not None
                assert any(record.action == "checkpoint.restore" for record in runtime.audit.trace())
                assert runtime.events.list()
            finally:
                runtime.close()

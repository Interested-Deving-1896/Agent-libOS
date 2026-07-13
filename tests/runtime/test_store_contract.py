from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from agent_libos.config import AgentLibOSConfig, RuntimeDefaults
from agent_libos.models import (
    CapabilityRight,
    ContextMaterializationManifest,
    EventType,
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    JsonRpcEndpointSpec,
    JsonRpcMethodSpec,
    LLMCallRecord,
    McpServerSpec,
    McpStdioTransportSpec,
    McpToolSpec,
    ObjectType,
    OperationOutcome,
    OperationState,
    ProcessMessageKind,
    ProcessStatus,
)
from agent_libos.runtime.runtime import Runtime
from agent_libos.sdk import (
    ProtectedOperationContract,
    ProtectedOperationInvocation,
    ResourcePolicy,
)
from agent_libos.utils.ids import utc_now
from agent_libos.models.exceptions import ValidationError


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
        operation = runtime.operations.start(
            kind="runtime",
            name="contract.operation",
            actor=pid,
            pid=pid,
            expected_roles=["result"],
        )
        assert runtime.operations.link_evidence(
            "result",
            "contract-operation-result",
            "result",
            operation_id=operation.operation_id,
        ) is not None
        assert runtime.operations.link_evidence(
            "result",
            "contract-operation-result",
            "result",
            operation_id=operation.operation_id,
        ) is None
        runtime.operations.finish("succeeded", operation_id=operation.operation_id)
        cas_operation = runtime.operations.start(
            kind="runtime",
            name="contract.cas",
            actor=pid,
            pid=pid,
        )
        runtime.operations.wait(operation_id=cas_operation.operation_id)
        waiting_snapshot = runtime.store.get_operation(cas_operation.operation_id)
        assert waiting_snapshot is not None
        assert runtime.operations.resume(cas_operation.operation_id).state == OperationState.RUNNING
        stale_terminal = replace(
            waiting_snapshot,
            state=OperationState.TERMINAL,
            outcome=OperationOutcome.SUCCEEDED,
            completed_at=utc_now(),
            updated_at=utc_now(),
        )
        assert runtime.store.update_operation(
            stale_terminal,
            expected_states=[OperationState.WAITING.value],
        ) is False
        runtime.operations.finish("succeeded", operation_id=cas_operation.operation_id)
        manifest = ContextMaterializationManifest(
            materialization_id="contract-context-manifest",
            pid=pid,
            view_id="contract-view",
            policy="contract",
            budget_tokens=64,
            rendered_tokens=12,
            rendered_sha256="a" * 64,
            context_generation="generation-1",
            context_oid=None,
            context_version=None,
            objects=[
                {
                    "oid": handle.oid,
                    "version": 1,
                    "type": ObjectType.ARTIFACT.value,
                    "disposition": "included",
                    "reason": "selected",
                    "transform": "verbatim",
                    "tokens": 12,
                    "rendered_sha256": "b" * 64,
                }
            ],
            created_at=utc_now(),
        )
        runtime.store.insert_context_materialization_manifest(manifest)

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
        stored_operation = runtime.store.get_operation(operation.operation_id)
        assert stored_operation is not None
        assert stored_operation.outcome.value == "succeeded"
        assert runtime.store.list_operation_evidence(operation_ids=[operation.operation_id])
        assert runtime.store.get_context_materialization_manifest(manifest.materialization_id) == manifest


@pytest.mark.parametrize("kind", STORE_BACKENDS)
def test_runtime_store_event_limit_returns_newest_matching_events_in_order(kind: str, tmp_path: Path) -> None:
    with _runtime_for_backend(kind, tmp_path) as runtime:
        pid = runtime.process.spawn(goal="bounded event history")
        emitted = [
            runtime.events.emit(
                EventType.EXTERNAL_WRITE,
                source="event-limit-test",
                target=pid,
                payload={"index": index},
            )
            for index in range(5)
        ]
        runtime.events.emit(
            EventType.EXTERNAL_WRITE,
            source="event-limit-test",
            target="another-process",
            payload={"index": 99},
        )

        selected = runtime.events.list(target=pid, limit=2)
        previous = runtime.events.list(target=pid, limit=2, before_event_id=selected[0].event_id)
        following = runtime.events.list(target=pid, limit=2, after_event_id=emitted[1].event_id)

        assert [event.event_id for event in selected] == [emitted[-2].event_id, emitted[-1].event_id]
        assert [event.payload["index"] for event in selected] == [3, 4]
        assert [event.event_id for event in previous] == [emitted[-4].event_id, emitted[-3].event_id]
        assert [event.payload["index"] for event in previous] == [1, 2]
        assert [event.event_id for event in following] == [emitted[2].event_id, emitted[3].event_id]


@pytest.mark.parametrize("kind", STORE_BACKENDS)
def test_runtime_store_process_snapshot_queries_are_bounded_and_batched(kind: str, tmp_path: Path) -> None:
    with _runtime_for_backend(kind, tmp_path) as runtime:
        parent = runtime.process.spawn(goal="snapshot parent")
        child = runtime.process.spawn_child(parent, goal="snapshot child")
        third = runtime.process.spawn(goal="snapshot third")
        terminal = runtime.process.get(third)
        terminal.status = ProcessStatus.EXITED
        runtime.store.update_process(terminal)
        messages = [
            runtime.messages.post(sender="test", recipient_pid=child, body=f"message-{index}")
            for index in range(3)
        ]
        interrupt = runtime.messages.post(
            sender="test",
            recipient_pid=child,
            kind=ProcessMessageKind.INTERRUPT,
            body="interrupt",
        )
        runtime.ratings.upsert(child, score=4, comment="batch")
        for index in range(3):
            runtime.store.insert_llm_call(
                LLMCallRecord(
                    call_id=f"snapshot-call-{index}",
                    pid=child,
                    image_id="base-agent:v0",
                    purpose="snapshot",
                    status="ok",
                    usage={"total_tokens": 10 + index},
                    created_at=utc_now(),
                )
            )

        listed = runtime.store.list_processes(limit=2)
        active_first = runtime.store.list_processes(limit=2, active_first=True)
        ancestors = runtime.store.get_processes_with_ancestors([child])
        activity = runtime.store.get_process_activity_summaries(
            [parent, child, third],
            recent_message_limit=2,
            recent_llm_call_limit=2,
        )
        ratings = runtime.store.get_agent_ratings_for_processes(
            [child, third],
            rater=runtime.config.runtime.default_human,
        )

        assert len(listed) == 2
        assert third not in {process.pid for process in active_first}
        assert {process.pid for process in ancestors} == {parent, child}
        assert activity[child]["unread_message_count"] == 4
        assert activity[child]["interrupt_count"] == 1
        assert activity[child]["llm_call_count"] == 2
        assert activity[child]["token_total"] == 23
        assert activity[parent]["llm_call_count"] == 0
        assert activity[parent]["token_total"] == 0
        assert [message.message_id for message in activity[child]["messages"]] == [
            messages[-1].message_id,
            interrupt.message_id,
        ]
        assert activity[third]["messages"] == []
        assert ratings[child].score == 4
        assert third not in ratings


def test_sqlite_file_store_rejects_concurrent_active_runtime_and_reopens_after_close(tmp_path: Path) -> None:
    db_path = tmp_path / "leased.sqlite"
    runtime = Runtime.open(db_path)
    try:
        with pytest.raises(ValidationError, match="already open"):
            Runtime.open(db_path)
    finally:
        runtime.close()

    reopened = Runtime.open(db_path)
    try:
        assert reopened.store is not None
    finally:
        reopened.close()


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


@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_prepared_protected_operation_restores_reservation_across_reopen(
    kind: str,
    tmp_path: Path,
) -> None:
    class Provider:
        def classify_external_effect(self, _operation, _context, _result):
            return ExternalEffectClassification(
                rollback_class=ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED,
                rollback_status=ExternalEffectRollbackStatus.NOT_REQUIRED,
                state_mutation=False,
                information_flow=True,
            )

    with _persistent_target(kind, tmp_path) as (target, config):
        runtime = Runtime.open(target, config=config)
        try:
            pid = runtime.process.spawn(goal=f"{kind} prepared recovery")
            capability = runtime.capability.issue_trusted(
                pid,
                "test:prepared-recovery",
                [CapabilityRight.READ],
                issued_by="test",
                uses_remaining=1,
            )
            decision = runtime.capability.require(
                pid,
                capability.resource,
                CapabilityRight.READ,
                consume=False,
            )
            contract = ProtectedOperationContract(
                name="primitive.test.prepared_recovery",
                provider="test",
                operation="read",
                evidence_roles=("audit", "event", "effect"),
                resource_policy=ResourcePolicy.NONE,
                information_flow=True,
            )
            runtime.protected_operations.register_contract(contract)
            invocation = ProtectedOperationInvocation(
                pid=pid,
                actor=pid,
                target=capability.resource,
                decisions=(decision,),
                canonical_args={"item": "secret"},
                observation={"item_sha256": "safe"},
            )
            operation = runtime.protected_operations.start(
                contract,
                invocation,
                provider=Provider(),
            )
            operation.__enter__()
            effect_id = operation.effect_id
            assert effect_id is not None
            scope = operation._operation_cm
            assert scope is not None
            interrupted = RuntimeError("simulated runtime crash")
            scope.__exit__(type(interrupted), interrupted, interrupted.__traceback__)
            operation._operation_cm = None
            runtime.store.close()
            runtime._closed = True

            reopened = Runtime.open(target, config=config)
            try:
                restored = reopened.store.get_capability(capability.cap_id)
                assert restored is not None
                assert restored.uses_remaining == 1
                assert reopened.store.get_external_effect(effect_id) is None
            finally:
                reopened.close()
        finally:
            if not runtime._closed:
                runtime.close()

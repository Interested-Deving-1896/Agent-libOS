from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from agent_libos.config import AgentLibOSConfig, RuntimeDefaults
from agent_libos.models import (
    AgentProcess,
    AuditRecord,
    CapabilityRight,
    ContextMaterializationManifest,
    DataFlowContext,
    DataFlowDecision,
    DataLabels,
    DataSourceRef,
    Event,
    EventPriority,
    EventType,
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    FileLabelBinding,
    HumanRequestStatus,
    JsonRpcEndpointSpec,
    JsonRpcMethodSpec,
    LLMCallRecord,
    McpServerSpec,
    McpStdioTransportSpec,
    McpToolSpec,
    ObjectMetadata,
    ObjectTaskStatus,
    ObjectType,
    OperationOutcome,
    OperationState,
    ProcessMessageKind,
    ProcessMessageStatus,
    ProcessStatus,
    ResourceBudget,
    ResourceUsage,
    SinkTrustSpec,
)
from agent_libos.runtime.runtime import Runtime
from agent_libos.sdk import (
    ProtectedOperationContract,
    ProtectedOperationInvocation,
    ResourcePolicy,
)
from agent_libos.utils.ids import utc_now
from agent_libos.models.exceptions import ProcessWaitRequired, ValidationError


STORE_BACKENDS = [
    "sqlite-memory",
    "sqlite-file",
    pytest.param("postgres", marks=pytest.mark.postgres),
]

PERSISTENT_STORE_BACKENDS = [
    "sqlite-file",
    pytest.param("postgres", marks=pytest.mark.postgres),
]


@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_reopen_fails_closed_stale_running_execution_with_audit(
    kind: str,
    tmp_path: Path,
) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        runtime = Runtime.open(target, config=config)
        pid = runtime.process.spawn(goal="leave a stale execution lease")
        token = runtime.store.claim_execution(pid, owner_id="runtime_that_crashed")
        assert token is not None
        runtime.close()

        reopened = Runtime.open(target, config=config)
        try:
            process = reopened.process.get(pid)
            assert process.status == ProcessStatus.PAUSED
            assert process.status_message == "stale_execution_recovery"
            assert process.execution_owner_id is None
            assert process.execution_lease_id is None
            assert process.execution_generation > token.generation
            records = reopened.audit.trace(target=f"process:{pid}")
            assert any(record.action == "stale_execution_recovery" for record in records)
        finally:
            reopened.close()


@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_reopen_compensates_incomplete_process_launch_publication(
    kind: str,
    tmp_path: Path,
) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        runtime = Runtime.open(target, config=config)
        now = utc_now()
        pid = f"pid-partial-{uuid4().hex}"
        publication_id = f"publication-partial-{uuid4().hex}"
        runtime.store.insert_runtime_publication(
            publication_id=publication_id,
            kind="process_launch",
            pid=pid,
            owner_instance_id="crashed-runtime",
            plan={"pid": pid, "launch_kind": "spawn", "image_id": "base-agent:v0"},
        )
        runtime.store.insert_process(
            AgentProcess(
                pid=pid,
                parent_pid=None,
                image_id="base-agent:v0",
                status=ProcessStatus.CREATED,
                goal_oid=None,
                memory_view=None,
                capabilities=[],
                loaded_skills={},
                tool_table={},
                event_cursor=None,
                checkpoint_head=None,
                resource_budget=ResourceBudget(),
                resource_usage=ResourceUsage(),
                created_at=now,
                updated_at=now,
            )
        )
        runtime.store.advance_runtime_publication(
            publication_id,
            state="applying",
            phase="process_inserted",
            expected_states={"planning"},
        )
        runtime.close()

        reopened = Runtime.open(target, config=config)
        try:
            assert reopened.store.get_process(pid) is None
            publication = reopened.store.get_runtime_publication(publication_id)
            assert publication is not None
            assert publication["state"] == "rolled_back"
            assert publication["phase"] == "startup_compensated"
        finally:
            reopened.close()


@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_reopen_compensates_incomplete_process_exec_publication(
    kind: str,
    tmp_path: Path,
) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        runtime = Runtime.open(target, config=config)
        pid = runtime.process.spawn(goal="before interrupted exec")
        before = runtime.process_exec_state.capture(pid)
        publication_id = f"publication-exec-{uuid4().hex}"
        runtime.store.insert_runtime_publication(
            publication_id=publication_id,
            kind="process_exec",
            pid=pid,
            owner_instance_id="crashed-runtime",
            plan={
                "pid": pid,
                "image_id": "coding-agent:v0",
                "before_snapshot": before.snapshot.to_mapping(),
                "before_tool_ids": sorted(before.tool_ids),
            },
        )
        process = runtime.process.get(pid)
        runtime.store.patch_process(
            pid,
            {"image_id": "coding-agent:v0"},
            expected_revision=process.revision,
        )
        runtime.store.advance_runtime_publication(
            publication_id,
            state="applying",
            phase="process_exec_applied",
            expected_states={"planning"},
        )
        runtime.close()

        reopened = Runtime.open(target, config=config)
        try:
            assert reopened.process.get(pid).image_id == "base-agent:v0"
            publication = reopened.store.get_runtime_publication(publication_id)
            assert publication is not None and publication["state"] == "rolled_back"
        finally:
            reopened.close()


@pytest.mark.parametrize("kind", STORE_BACKENDS)
def test_runtime_publication_terminal_phase_preserves_original_error(
    kind: str,
    tmp_path: Path,
) -> None:
    with _runtime_for_backend(kind, tmp_path) as runtime:
        publication_id = f"publication-error-{uuid4().hex}"
        pid = f"pid-error-{uuid4().hex}"
        runtime.store.insert_runtime_publication(
            publication_id=publication_id,
            kind="process_launch",
            pid=pid,
            owner_instance_id="test-runtime",
            plan={"pid": pid},
        )
        assert runtime.store.advance_runtime_publication(
            publication_id,
            state="rollback_pending",
            phase="compensating",
            error={"code": "launch_failed", "error_type": "InjectedFailure"},
            expected_states={"planning"},
        )
        assert runtime.store.advance_runtime_publication(
            publication_id,
            state="rolled_back",
            phase="compensated",
            receipt={"phase": "compensated", "pid": pid},
            expected_states={"rollback_pending"},
        )

        publication = runtime.store.get_runtime_publication(publication_id)
        assert publication is not None
        assert publication["error"] == {
            "code": "launch_failed",
            "error_type": "InjectedFailure",
        }


@pytest.mark.parametrize("kind", STORE_BACKENDS)
def test_llm_call_limit_selects_latest_window_then_returns_chronologically(
    kind: str,
    tmp_path: Path,
) -> None:
    with _runtime_for_backend(kind, tmp_path) as runtime:
        records = [
            ("call-old", "pid-a", "2026-01-01T00:00:01+00:00"),
            ("call-tie-a", "pid-a", "2026-01-01T00:00:02+00:00"),
            ("call-tie-b", "pid-a", "2026-01-01T00:00:02+00:00"),
            ("call-new", "pid-a", "2026-01-01T00:00:03+00:00"),
            ("call-global-new", "pid-b", "2026-01-01T00:00:04+00:00"),
        ]
        for call_id, pid, created_at in records:
            runtime.store.insert_llm_call(
                LLMCallRecord(
                    call_id=call_id,
                    pid=pid,
                    image_id=None,
                    purpose="latest-window",
                    status="ok",
                    created_at=created_at,
                )
            )

        assert [call.call_id for call in runtime.store.list_llm_calls(limit=3)] == [
            "call-tie-b",
            "call-new",
            "call-global-new",
        ]
        assert [call.call_id for call in runtime.store.list_llm_calls(pid="pid-a", limit=2)] == [
            "call-tie-b",
            "call-new",
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
def test_runtime_store_contract_data_flow_registry_decisions_and_file_labels(
    kind: str,
    tmp_path: Path,
) -> None:
    with _runtime_for_backend(kind, tmp_path) as runtime:
        now = utc_now()
        source = DataSourceRef("contract-source", 1, "a" * 64)
        labels = DataLabels(
            sensitivity="secret",
            trust_level="verified",
            integrity="checked",
            tenant="tenant-a",
            principal="principal-a",
        )
        trust = SinkTrustSpec(
            trust_id="contract-trust-v1",
            pattern="filesystem:workspace:secure/*",
            trust_level="trusted",
            max_sensitivity="secret",
            generation=1,
            created_by="test",
            created_at=now,
            tenants=("tenant-a",),
            principals=("principal-a",),
        )

        assert runtime.store.get_sink_trust_generation() == 0
        assert runtime.store.register_sink_trust(trust) == trust
        assert runtime.store.get_sink_trust_generation() == 1
        assert runtime.store.inspect_sink_trust(trust.pattern) == trust

        replacement = SinkTrustSpec(
            trust_id="contract-trust-v2",
            pattern=trust.pattern,
            trust_level="trusted",
            max_sensitivity="restricted",
            generation=2,
            created_by="test",
            created_at=utc_now(),
            tenants=("tenant-a",),
            principals=("principal-a",),
        )
        with pytest.raises(ValidationError, match="already exists"):
            runtime.store.register_sink_trust(replacement)
        assert runtime.store.get_sink_trust_generation() == 1
        assert runtime.store.inspect_sink_trust(trust.pattern) == trust

        runtime.store.register_sink_trust(replacement, replace=True)
        historical = runtime.store.get_sink_trust(trust.trust_id)
        assert historical is not None
        assert historical.active is False
        assert historical.deactivated_at == replacement.created_at
        assert runtime.store.inspect_sink_trust(trust.pattern) == replacement
        assert runtime.store.list_sink_trust() == [replacement]

        decision = DataFlowDecision(
            decision_id="contract-data-flow-decision",
            pid="contract-pid",
            sink="filesystem:workspace:secure/result.txt",
            direction="egress",
            outcome="allow",
            reason="trusted_sink_clearance",
            labels=labels,
            source_refs=(source,),
            payload_hash="b" * 64,
            trust_id=replacement.trust_id,
            trust_hash=replacement.spec_hash,
            registry_generation=2,
            created_at=utc_now(),
        )
        runtime.store.insert_data_flow_decision(decision)
        assert runtime.store.get_data_flow_decision(decision.decision_id) == decision
        assert runtime.store.list_data_flow_decisions(pid=decision.pid) == [decision]
        decision_row = runtime.store.select_table_rows(
            "data_flow_decisions",
            "decision_id = ?",
            (decision.decision_id,),
        )[0]
        assert "payload_json" not in decision_row
        assert "payload" not in decision_row

        first = FileLabelBinding(
            binding_id="contract-file-label-v1",
            normalized_path="secure/result.txt",
            content_sha256="c" * 64,
            labels=labels,
            source_refs=(source,),
            generation=1,
            tombstoned=False,
            active=True,
            created_by="contract-pid",
            created_at=utc_now(),
        )
        runtime.store.upsert_file_label_binding(first)
        stale = replace(first, binding_id="contract-file-label-stale", content_sha256="e" * 64)
        with pytest.raises(ValidationError, match="generation conflict"):
            runtime.store.upsert_file_label_binding(stale)
        assert runtime.store.get_file_label_binding(first.normalized_path) == first
        assert runtime.store.get_file_label_binding_by_id(first.binding_id) == first

        second = FileLabelBinding(
            binding_id="contract-file-label-v2",
            normalized_path=first.normalized_path,
            content_sha256="d" * 64,
            labels=labels,
            source_refs=(source,),
            generation=2,
            tombstoned=False,
            active=True,
            created_by="contract-pid",
            created_at=utc_now(),
        )
        runtime.store.upsert_file_label_binding(second)

        assert runtime.store.get_file_label_binding(first.normalized_path) == second
        assert runtime.store.get_file_label_binding_by_id(first.binding_id) == replace(
            first,
            active=False,
            superseded_at=second.created_at,
        )
        assert runtime.store.get_file_label_binding_by_id(second.binding_id) == second
        assert runtime.store.get_file_label_binding_generation(first.normalized_path) == 2
        history = runtime.store.list_file_label_bindings(
            normalized_path=first.normalized_path,
            include_history=True,
        )
        assert [item.binding_id for item in history] == [second.binding_id, first.binding_id]
        assert history[1].active is False

        tombstone = runtime.store.tombstone_file_label_binding(
            first.normalized_path,
            binding_id="contract-file-label-tombstone",
            created_by="test",
            created_at=utc_now(),
        )
        assert tombstone is not None
        assert tombstone.tombstoned is True
        assert runtime.store.get_file_label_binding(first.normalized_path) is None
        assert runtime.store.get_file_label_binding_by_id(second.binding_id) == replace(
            second,
            active=False,
            superseded_at=tombstone.created_at,
        )
        assert runtime.store.get_file_label_binding_by_id(tombstone.binding_id) == tombstone
        assert runtime.store.get_file_label_binding_generation(first.normalized_path) == 3
        assert runtime.store.list_file_label_bindings(
            normalized_path=first.normalized_path,
            include_history=True,
            include_tombstones=True,
        )[0] == tombstone

        assert runtime.store.unregister_sink_trust(
            replacement.pattern,
            generation=3,
            deactivated_at=utc_now(),
        )
        assert runtime.store.get_sink_trust_generation() == 3
        assert runtime.store.inspect_sink_trust(replacement.pattern) is None


@pytest.mark.parametrize("kind", STORE_BACKENDS)
def test_legacy_process_message_envelope_marker_remains_user_payload(
    kind: str,
    tmp_path: Path,
) -> None:
    with _runtime_for_backend(kind, tmp_path) as runtime:
        pid = runtime.process.spawn(image="base-agent:v0", goal="legacy message payload")
        message = runtime.messages.post(
            sender="test.host",
            recipient_pid=pid,
            payload={"placeholder": True},
            metadata={"custom": "preserved"},
        )
        legacy_payload = {
            "__agent_libos_process_message_v1__": True,
            "payload": {"original": "user-data"},
            "metadata": {"data_labels": {"sensitivity": "secret"}},
        }
        runtime.store._execute(
            "UPDATE process_messages SET payload_json = ? WHERE message_id = ?",
            (json.dumps(legacy_payload), message.message_id),
        )

        decoded = runtime.store.get_process_message(message.message_id)

        assert decoded is not None
        assert decoded.payload == legacy_payload
        assert decoded.metadata["custom"] == "preserved"




@pytest.mark.parametrize(
    "raw_context",
    [
        pytest.param(json.dumps({"labels": {"trust_level": "trusted"}}), id="partial"),
        pytest.param("{malformed-json", id="malformed"),
    ],
)
@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_noncanonical_pending_flow_context_fails_closed_on_reopen(
    kind: str,
    raw_context: str,
    tmp_path: Path,
) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        assert target is not None
        runtime = Runtime.open(target, config=config)
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="invalid pending context")
            runtime.store.upsert_llm_pending_action(
                pid,
                {
                    "resume_token": "invalid-pending-context-token",
                    "wait_type": "message",
                    "filters": {},
                    "action": {"action": "receive_process_messages"},
                    "data_flow_context": DataFlowContext().to_dict(),
                    "content_preview": "",
                    "tool_call_count": 1,
                    "status": "pending",
                },
            )
        finally:
            runtime.close()

        if kind == "sqlite-file":
            with sqlite3.connect(target) as connection:
                connection.execute(
                    "UPDATE llm_pending_actions SET data_flow_context_json = ? WHERE pid = ?",
                    (raw_context, pid),
                )
                connection.commit()
        else:
            import psycopg

            with psycopg.connect(str(target), autocommit=True) as connection:
                connection.execute(
                    "UPDATE llm_pending_actions SET data_flow_context_json = %s WHERE pid = %s",
                    (raw_context, pid),
                )

        with pytest.raises(ValidationError, match="pending LLM action"):
            Runtime.open(target, config=config)


@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_minimal_complete_pending_flow_context_is_normalized_without_failure(
    kind: str,
    tmp_path: Path,
) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        assert target is not None
        runtime = Runtime.open(target, config=config)
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="minimal pending context")
            runtime.store.upsert_llm_pending_action(
                pid,
                {
                    "resume_token": "minimal-pending-context-token",
                    "wait_type": "message",
                    "filters": {},
                    "action": {"action": "receive_process_messages"},
                    "data_flow_context": DataFlowContext().to_dict(),
                    "content_preview": "",
                    "tool_call_count": 1,
                    "status": "pending",
                },
            )
        finally:
            runtime.close()

        minimal = json.dumps(
            {
                "labels": {
                    "sensitivity": "normal",
                    "trust_level": "trusted",
                    "integrity": "verified",
                }
            }
        )
        if kind == "sqlite-file":
            with sqlite3.connect(target) as connection:
                connection.execute(
                    "UPDATE llm_pending_actions SET data_flow_context_json = ? WHERE pid = ?",
                    (minimal, pid),
                )
                connection.commit()
        else:
            import psycopg

            with psycopg.connect(str(target), autocommit=True) as connection:
                connection.execute(
                    "UPDATE llm_pending_actions SET data_flow_context_json = %s WHERE pid = %s",
                    (minimal, pid),
                )

        reopened = Runtime.open(target, config=config)
        try:
            assert reopened.process.get(pid).status == ProcessStatus.RUNNABLE
            pending = reopened.store.get_llm_pending_action(pid)
            assert pending is not None
            assert pending["status"] == "pending"
            labels = DataFlowContext.from_dict(pending["data_flow_context"]).labels
            assert labels.sensitivity.value == "normal"
            assert labels.trust_level.value == "trusted"
            assert labels.integrity.value == "verified"
        finally:
            reopened.close()




def test_released_process_result_wraps_corrupt_metadata_as_validation_error(
    tmp_path: Path,
) -> None:
    target = tmp_path / "corrupt-released-result.sqlite"
    runtime = Runtime.open(target)
    try:
        parent = runtime.process.spawn(image="base-agent:v0", goal="wait corrupt result")
        child = runtime.process.spawn_child(parent, "produce corrupt result")
        result = runtime.memory.create_object(
            child,
            ObjectType.SUMMARY,
            {"done": True},
            name="corrupt.process.result",
        )
        runtime.process.exit(child, result=result)
    finally:
        runtime.close()

    with sqlite3.connect(target) as connection:
        connection.execute(
            "UPDATE objects SET metadata_json = ? WHERE oid = ?",
            ("{malformed-json", result.oid),
        )
        connection.commit()

    reopened = Runtime.open(target)
    try:
        with pytest.raises(
            ValidationError,
            match=f"invalid persisted metadata for released object {result.oid}",
        ):
            reopened.process.wait(parent, child)
    finally:
        reopened.close()




@pytest.mark.parametrize("kind", STORE_BACKENDS)
def test_pending_action_rejects_incomplete_flow_labels(
    kind: str,
    tmp_path: Path,
) -> None:
    with _runtime_for_backend(kind, tmp_path) as runtime:
        pid = runtime.process.spawn(image="base-agent:v0", goal="reject partial pending labels")

        with pytest.raises(ValidationError, match="complete security labels"):
            runtime.store.upsert_llm_pending_action(
                pid,
                {
                    "wait_type": "message",
                    "filters": {},
                    "action": {"action": "receive_process_messages"},
                    "data_flow_context": {"labels": {"trust_level": "untrusted"}},
                    "status": "pending",
                },
            )

        assert runtime.store.get_llm_pending_action(pid) is None


@pytest.mark.parametrize("kind", STORE_BACKENDS)
def test_gui_snapshot_visibility_filters_before_limit_with_stable_ties(
    kind: str,
    tmp_path: Path,
) -> None:
    event_specs = [
        ("gui-event-00", EventType.PROCESS_CREATED, {"marker": "visible"}),
        ("gui-event-01", EventType.HUMAN_OUTPUT, {"purpose": "gui_presentation"}),
        ("gui-event-02", EventType.HUMAN_OUTPUT, {"purpose": "gui_presentation_extra"}),
        ("gui-event-03", EventType.DATA_FLOW_DECISION, {"sink": "human:owner:gui"}),
        ("gui-event-04", EventType.DATA_FLOW_DECISION, {"sink": "human:owner:gui-extra"}),
        ("gui-event-05", EventType.HUMAN_OUTPUT, {"purpose": "terminal_output"}),
        ("gui-event-06", EventType.PROCESS_SIGNAL, {"signal": "resume"}),
    ]
    audit_specs = [
        ("gui-audit-00", "test.visible", "process:pid", {"marker": "visible"}),
        ("gui-audit-01", "human.output", "human:owner:terminal", {"purpose": "gui_presentation"}),
        ("gui-audit-02", "human.output", "human:owner:terminal", {"purpose": "gui_presentation_extra"}),
        ("gui-audit-03", "data_flow.egress", "human:owner:gui", {"sink": "human:owner:gui"}),
        ("gui-audit-04", "data_flow.egress", "human:owner:gui-extra", {"sink": "human:owner:gui-extra"}),
        ("gui-audit-05", "data_flow.egress", "service:archive", {"sink": "service:archive"}),
        ("gui-audit-06", "test.visible", "process:pid", None),
    ]

    with _runtime_for_backend(kind, tmp_path) as runtime:
        timestamp = utc_now()
        for event_id, event_type, payload in event_specs:
            runtime.store.insert_event(
                Event(
                    event_id=event_id,
                    type=event_type,
                    source="test",
                    target="pid",
                    payload=payload,
                    priority=EventPriority.NORMAL,
                    created_at=timestamp,
                )
            )
        for record_id, action, target, decision in audit_specs:
            runtime.store.insert_audit(
                AuditRecord(
                    record_id=record_id,
                    timestamp=timestamp,
                    actor="test",
                    action=action,
                    target=target,
                    input_refs=[],
                    output_refs=[],
                    capability_refs=[],
                    decision=decision,
                    correlation_id=None,
                )
            )

        events = runtime.store.list_events(
            limit=3,
            include_gui_presentation=False,
        )
        event_page_one = runtime.store.list_events(
            limit=2,
            after_event_id="gui-event-00",
            include_gui_presentation=False,
        )
        event_page_two = runtime.store.list_events(
            limit=2,
            after_event_id="gui-event-04",
            include_gui_presentation=False,
        )
        audit = runtime.store.list_audit(
            limit=3,
            include_gui_presentation=False,
        )
        audit_match_any = runtime.store.list_audit(
            actor="test",
            target="human:owner:terminal",
            match_any=True,
            include_gui_presentation=False,
        )

        assert [item.event_id for item in events] == [
            "gui-event-04",
            "gui-event-05",
            "gui-event-06",
        ]
        assert [item.event_id for item in event_page_one] == [
            "gui-event-02",
            "gui-event-04",
        ]
        assert [item.event_id for item in event_page_two] == [
            "gui-event-05",
            "gui-event-06",
        ]
        assert [item.record_id for item in audit] == [
            "gui-audit-04",
            "gui-audit-05",
            "gui-audit-06",
        ]
        assert [item.record_id for item in audit_match_any] == [
            "gui-audit-00",
            "gui-audit-02",
            "gui-audit-04",
            "gui-audit-05",
            "gui-audit-06",
        ]
        all_event_ids = {item.event_id for item in runtime.store.list_events()}
        all_audit_ids = {item.record_id for item in runtime.store.list_audit()}
        assert {item[0] for item in event_specs}.issubset(all_event_ids)
        assert {item[0] for item in audit_specs}.issubset(all_audit_ids)




def test_sqlite_tree_file_label_query_is_indexed_and_batched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_config = AgentLibOSConfig()
    config = replace(
        base_config,
        data_flow=replace(base_config.data_flow, file_binding_list_limit=2),
    )
    runtime = Runtime.open(tmp_path / "tree-labels.sqlite", config=config)
    try:
        labels = DataLabels(sensitivity="confidential")
        paths = [
            "tree",
            "tree/a.txt",
            "tree/nested/b.txt",
            "tree/nested/c.txt",
            "tree/z.txt",
            "treehouse/outside.txt",
        ]
        for index, path in enumerate(paths):
            runtime.store.upsert_file_label_binding(
                FileLabelBinding(
                    binding_id=f"tree-binding-{index}",
                    normalized_path=path,
                    content_sha256=f"{index + 1:064x}",
                    labels=labels,
                    source_refs=(),
                    generation=1,
                    tombstoned=False,
                    active=True,
                    created_by="test",
                    created_at=utc_now(),
                )
            )

        original_query = runtime.store._query
        captured: list[tuple[str, tuple[object, ...]]] = []

        def tracked_query(sql: str, params: object = ()) -> list[object]:
            selected_params = tuple(params)  # type: ignore[arg-type]
            if "FROM file_label_bindings" in sql and "LIMIT" in sql:
                captured.append((sql, selected_params))
            return original_query(sql, selected_params)

        monkeypatch.setattr(runtime.store, "_query", tracked_query)

        found = runtime.store.list_file_label_bindings_for_tree("tree")

        assert [binding.normalized_path for binding in found] == paths[:-1]
        assert len(captured) >= 3
        plan = list(
            runtime.store.conn.execute(
                f"EXPLAIN QUERY PLAN {captured[0][0]}",
                captured[0][1],
            )
        )
        details = "\n".join(str(row[3]) for row in plan)
        assert "SEARCH file_label_bindings USING INDEX idx_file_label_tree_scan" in details
        assert "normalized_path>?" in details or "normalized_path>? AND normalized_path<?" in details
    finally:
        runtime.close()


@pytest.mark.parametrize("kind", STORE_BACKENDS)
def test_tree_file_label_prefix_is_backend_collation_safe(
    kind: str,
    tmp_path: Path,
) -> None:
    with _runtime_for_backend(kind, tmp_path) as runtime:
        root = "树_%!"
        expected = [root, f"{root}/é.txt", f"{root}/子/ß.txt"]
        for index, path in enumerate(
            [*expected, f"{root}house/outside.txt", "树/outside.txt"]
        ):
            runtime.store.upsert_file_label_binding(
                FileLabelBinding(
                    binding_id=f"collation-tree-binding-{index}",
                    normalized_path=path,
                    content_sha256=f"{index + 1:064x}",
                    labels=DataLabels(sensitivity="confidential"),
                    source_refs=(),
                    generation=1,
                    tombstoned=False,
                    active=True,
                    created_by="test",
                    created_at=utc_now(),
                )
            )

        found = runtime.store.list_file_label_bindings_for_tree(root)

        assert [binding.normalized_path for binding in found] == expected


@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_data_flow_evidence_and_label_bindings_persist_across_reopen(
    kind: str,
    tmp_path: Path,
) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        created_at = utc_now()
        source = DataSourceRef("persisted-source", 3, "a" * 64)
        labels = DataLabels(sensitivity="restricted", tenant="tenant-a")
        trust = SinkTrustSpec(
            trust_id="persisted-trust",
            pattern="filesystem:workspace:archive/*",
            trust_level="trusted",
            max_sensitivity="restricted",
            generation=1,
            created_by="test",
            created_at=created_at,
            tenants=("tenant-a",),
        )
        decision = DataFlowDecision(
            decision_id="persisted-decision",
            pid="persisted-pid",
            sink="filesystem:workspace:archive/result.txt",
            direction="egress",
            outcome="allow",
            reason="trusted_sink_clearance",
            labels=labels,
            source_refs=(source,),
            payload_hash="b" * 64,
            registry_generation=1,
            created_at=created_at,
            trust_id=trust.trust_id,
            trust_hash=trust.spec_hash,
        )
        binding = FileLabelBinding(
            binding_id="persisted-file-label",
            normalized_path="archive/result.txt",
            content_sha256="c" * 64,
            labels=labels,
            source_refs=(source,),
            generation=1,
            tombstoned=False,
            active=True,
            created_by="persisted-pid",
            created_at=created_at,
        )

        runtime = Runtime.open(target, config=config)
        try:
            runtime.store.register_sink_trust(trust)
            runtime.store.insert_data_flow_decision(decision)
            runtime.store.upsert_file_label_binding(binding)
        finally:
            runtime.close()

        reopened = Runtime.open(target, config=config)
        try:
            assert reopened.store.get_sink_trust_generation() == 1
            assert reopened.store.get_sink_trust(trust.trust_id) == trust
            assert reopened.store.get_data_flow_decision(decision.decision_id) == decision
            assert reopened.store.get_file_label_binding(binding.normalized_path) == binding
        finally:
            reopened.close()


@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_llm_context_label_history_persists_and_merges_monotonically(
    kind: str,
    tmp_path: Path,
) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        runtime = Runtime.open(target, config=config)
        store_closed = False
        try:
            first = runtime.store.merge_llm_context_label_history(
                "pid-context-labels",
                DataLabels(
                    sensitivity="restricted",
                    tenant="tenant-a",
                    principal="analyst-a",
                ),
            )
            merged = runtime.store.merge_llm_context_label_history(
                "pid-context-labels",
                DataLabels(
                    sensitivity="secret",
                    tenant="tenant-b",
                    principal="analyst-a",
                ),
            )

            assert first.sensitivity.value == "restricted"
            assert merged.sensitivity.value == "secret"
            assert merged.tenant == "mixed"
            assert merged.principal == "analyst-a"
        finally:
            runtime.close()

        reopened = Runtime.open(target, config=config)
        try:
            restored = reopened.store.get_llm_context_label_history(
                "pid-context-labels"
            )
            assert restored == merged
        finally:
            reopened.close()


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
            assert json.loads(row["payload_json"]) == {
                "storage": "runtime_memory",
                "present": False,
                "recovered_after_reopen": True,
            }
            assert reopened.store.is_recovered_object_payload(handle.oid)
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
            store_closed = True

            reopened = Runtime.open(target, config=config)
            try:
                restored = reopened.store.get_capability(capability.cap_id)
                assert restored is not None
                assert restored.uses_remaining == 1
                assert reopened.store.get_external_effect(effect_id) is None
            finally:
                reopened.close()
        finally:
            if not store_closed:
                runtime.close()

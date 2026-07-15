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
    SinkTrustSpec,
)
from agent_libos.runtime.runtime import Runtime
from agent_libos.storage.base import LEGACY_PENDING_DATA_FLOW_MESSAGE
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


@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_legacy_flow_columns_migrate_conservatively(
    kind: str,
    tmp_path: Path,
) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        assert target is not None
        runtime = Runtime.open(target, config=config)
        try:
            parent_pid = runtime.process.spawn(image="base-agent:v0", goal="legacy durable flow parent")
            pid = runtime.process.spawn_child(parent_pid, goal="legacy durable flow state")
            runtime.capability.grant_once(
                pid,
                "human:owner",
                [CapabilityRight.WRITE],
                issued_by="test",
            )
            request_id = runtime.human.ask(pid, "legacy request must not survive migration")
            with pytest.raises(ProcessWaitRequired):
                runtime.process.wait(parent_pid, pid)
            assert runtime.store.list_resource_reservations(child_pid=pid)
            message = runtime.messages.post(
                sender="legacy.sender",
                recipient_pid=pid,
                subject="legacy message",
                body="content whose historical classification is unavailable",
            )
            runtime.store.upsert_llm_pending_action(
                pid,
                {
                    "resume_token": "legacy-flow-token",
                    "wait_type": "message",
                    "filters": {"channel": "legacy"},
                    "action": {"action": "receive_process_messages", "channel": "legacy"},
                    "data_flow_context": DataFlowContext(
                        labels=DataLabels(
                            sensitivity="secret",
                            trust_level="untrusted",
                            integrity="untrusted",
                        )
                    ).to_dict(),
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
                    "ALTER TABLE llm_pending_actions DROP COLUMN data_flow_context_json"
                )
                connection.execute("ALTER TABLE process_messages DROP COLUMN metadata_json")
                connection.commit()
        else:
            import psycopg

            with psycopg.connect(str(target), autocommit=True) as connection:
                connection.execute(
                    "ALTER TABLE llm_pending_actions DROP COLUMN data_flow_context_json"
                )
                connection.execute("ALTER TABLE process_messages DROP COLUMN metadata_json")

        reopened = Runtime.open(target, config=config)
        try:
            pending = reopened.store.get_llm_pending_action(pid)
            assert pending is not None
            assert pending["status"] == "legacy_data_flow_reconciled"
            pending_context = DataFlowContext.from_dict(pending["data_flow_context"])
            assert pending_context.labels.sensitivity.value == "secret"
            assert pending_context.labels.trust_level.value == "untrusted"
            assert reopened.process.get(pid).status == ProcessStatus.FAILED
            assert reopened.process.get(parent_pid).status == ProcessStatus.RUNNABLE
            assert reopened.store.list_resource_reservations(child_pid=pid) == []
            assert reopened.human.get(request_id).status == HumanRequestStatus.CANCELLED
            assert request_id not in {item.request_id for item in reopened.human.pending()}
            assert pid not in reopened.llm._pending_message_actions

            next_pid = reopened.process.spawn(image="base-agent:v0", goal="later human request")
            reopened.capability.grant_once(
                next_pid,
                "human:owner",
                [CapabilityRight.WRITE],
                issued_by="test",
            )
            next_request_id = reopened.human.ask(next_pid, "continue after migrated request")
            processed = reopened.human.process_next_terminal(auto_answer="continue")
            assert processed is not None
            assert processed.request_id == next_request_id

            restored_message = reopened.store.get_process_message(message.message_id)
            assert restored_message is not None
            assert restored_message.status == ProcessMessageStatus.UNREAD
            assert restored_message.metadata["data_labels"]["sensitivity"] == "secret"
            assert restored_message.metadata["data_labels"]["trust_level"] == "untrusted"
            assert restored_message.metadata["data_labels"]["integrity"] == "untrusted"
        finally:
            reopened.close()


def test_legacy_pending_terminal_reconciliation_resumes_after_claim(
    tmp_path: Path,
) -> None:
    target = tmp_path / "legacy-reconciling.sqlite"
    runtime = Runtime.open(target)
    try:
        pid = runtime.process.spawn(image="base-agent:v0", goal="resume legacy cleanup")
        runtime.capability.grant_once(
            pid,
            "human:owner",
            [CapabilityRight.WRITE],
            issued_by="test",
        )
        request_id = runtime.human.ask(pid, "cancel me after claimed migration")
        runtime.store.upsert_llm_pending_action(
            pid,
            {
                "resume_token": "legacy-claimed-token",
                "wait_type": "human",
                "request_id": request_id,
                "filters": {},
                "action": {"action": "ask_human"},
                "data_flow_context": DataFlowContext().to_dict(),
                "content_preview": "",
                "tool_call_count": 1,
                "status": "pending",
            },
        )
    finally:
        runtime.close()

    # Simulate a crash after the atomic process transition/claim committed but
    # before manager cleanup and marker completion.
    with sqlite3.connect(target) as connection:
        connection.execute(
            "UPDATE llm_pending_actions SET status = ? WHERE pid = ?",
            ("legacy_data_flow_reconciling", pid),
        )
        connection.execute(
            "UPDATE processes SET status = ?, status_message = ? WHERE pid = ?",
            (ProcessStatus.FAILED.value, LEGACY_PENDING_DATA_FLOW_MESSAGE, pid),
        )
        connection.commit()

    reopened = Runtime.open(target)
    try:
        pending = reopened.store.get_llm_pending_action(pid)
        assert pending is not None
        assert pending["status"] == "legacy_data_flow_reconciled"
        assert reopened.human.get(request_id).status == HumanRequestStatus.CANCELLED
        assert request_id not in {item.request_id for item in reopened.human.pending()}
    finally:
        reopened.close()


def test_historical_terminal_reconciliation_still_runs_manager_cleanup(
    tmp_path: Path,
) -> None:
    target = tmp_path / "historical-terminal-reconciling.sqlite"
    runtime = Runtime.open(target)
    try:
        pid = runtime.process.spawn(image="base-agent:v0", goal="historical terminal cleanup")
        runtime.capability.grant_once(
            pid,
            "human:owner",
            [CapabilityRight.WRITE],
            issued_by="test",
        )
        request_id = runtime.human.ask(pid, "cancel after historical terminal crash")
        runtime.store.upsert_llm_pending_action(
            pid,
            {
                "resume_token": "historical-terminal-token",
                "wait_type": "human",
                "request_id": request_id,
                "filters": {},
                "action": {"action": "ask_human"},
                "data_flow_context": DataFlowContext().to_dict(),
                "content_preview": "",
                "tool_call_count": 1,
                "status": "pending",
            },
        )
    finally:
        runtime.close()

    with sqlite3.connect(target) as connection:
        connection.execute(
            "UPDATE llm_pending_actions SET status = ? WHERE pid = ?",
            ("legacy_data_flow_reconciling", pid),
        )
        connection.execute(
            "UPDATE processes SET status = ?, status_message = ? WHERE pid = ?",
            (ProcessStatus.KILLED.value, "historical terminal reason", pid),
        )
        connection.commit()

    reopened = Runtime.open(target)
    try:
        pending = reopened.store.get_llm_pending_action(pid)
        assert pending is not None
        assert pending["status"] == "legacy_data_flow_reconciled"
        assert reopened.human.get(request_id).status == HumanRequestStatus.CANCELLED
        assert request_id not in {item.request_id for item in reopened.human.pending()}
    finally:
        reopened.close()


def test_historical_terminal_reconciliation_notifies_waiting_object_task() -> None:
    runtime = Runtime.open("local")
    try:
        creator_pid = runtime.process.spawn(
            image="base-agent:v0",
            goal="observe historical child terminal cleanup",
        )
        runtime.capability.grant(
            creator_pid,
            "process:spawn",
            [CapabilityRight.WRITE],
            issued_by="test",
        )
        owner = runtime.memory.create_object(
            creator_pid,
            ObjectType.ARTIFACT,
            {"task": "wait for migrated child"},
            name="historical.terminal.object-task-owner",
            metadata=ObjectMetadata(title="historical terminal owner"),
            immutable=False,
        )
        task = runtime.object_tasks.start(
            creator_pid,
            owner,
            "receive_process_messages",
            {"channel": "never"},
        )
        waiting = runtime.object_tasks.wait(task.task_id, actor_pid=creator_pid, timeout=2)
        assert waiting.status == ObjectTaskStatus.WAITING_MESSAGE
        runner_pid = str(waiting.runner_pid)
        runtime.capability.grant(
            runner_pid,
            "process:spawn",
            [CapabilityRight.WRITE],
            issued_by="test",
        )
        child_pid = runtime.spawn_child_process(
            runner_pid,
            "historical terminal child",
        )
        runtime.tools.configure_process_tools(
            runner_pid,
            ["wait_child_process"],
            assigned_by="test",
        )
        runtime.object_tasks._pending_args[task.task_id] = {"child_pid": child_pid}
        runtime.store.update_object_task(
            replace(
                waiting,
                status=ObjectTaskStatus.WAITING_PROCESS,
                tool="wait_child_process",
                wait={"child_pid": child_pid},
            )
        )
        runtime.store.upsert_llm_pending_action(
            child_pid,
            {
                "resume_token": "historical-object-task-token",
                "wait_type": "message",
                "filters": {},
                "action": {"action": "receive_process_messages"},
                "data_flow_context": DataFlowContext().to_dict(),
                "content_preview": "",
                "tool_call_count": 1,
                "status": "pending",
            },
        )
        child = runtime.process.get(child_pid)
        child.status = ProcessStatus.KILLED
        child.status_message = "historical object-task terminal reason"
        child.updated_at = utc_now()
        runtime.store.update_process(child)
        runtime.store._execute(
            "UPDATE llm_pending_actions SET status = ? WHERE pid = ?",
            ("legacy_data_flow_reconciling", child_pid),
        )

        runtime._reconcile_legacy_pending_action_terminals()
        completed = runtime.object_tasks.wait(
            task.task_id,
            actor_pid=creator_pid,
            timeout=2,
        )

        assert completed.status == ObjectTaskStatus.SUCCEEDED
        assert any(
            record.action == "object_task.process_resume"
            and record.target == f"object_task:{task.task_id}"
            for record in runtime.audit.trace()
        )
        pending = runtime.store.get_llm_pending_action(child_pid)
        assert pending is not None
        assert pending["status"] == "legacy_data_flow_reconciled"
    finally:
        runtime.close()


def test_historical_terminal_reconciliation_preserves_result_object() -> None:
    runtime = Runtime.open("local")
    try:
        pid = runtime.process.spawn(image="base-agent:v0", goal="preserve historical result")
        result = runtime.memory.create_object(
            pid,
            ObjectType.SUMMARY,
            {"result": True},
            name="historical.result",
        )
        transient = runtime.memory.create_object(
            pid,
            ObjectType.OBSERVATION,
            {"transient": True},
            name="historical.transient",
        )
        runtime.store.upsert_llm_pending_action(
            pid,
            {
                "resume_token": "historical-result-token",
                "wait_type": "message",
                "filters": {},
                "action": {"action": "receive_process_messages"},
                "data_flow_context": DataFlowContext().to_dict(),
                "content_preview": "",
                "tool_call_count": 1,
                "status": "pending",
            },
        )
        process = runtime.process.get(pid)
        process.status = ProcessStatus.EXITED
        process.status_message = f"result_oid:{result.oid}"
        runtime.store.update_process(process)
        runtime.store._execute(
            "UPDATE llm_pending_actions SET status = ? WHERE pid = ?",
            ("legacy_data_flow_reconciling", pid),
        )

        runtime._reconcile_legacy_pending_action_terminals()

        assert runtime.store.get_object(result.oid) is not None
        assert runtime.store.get_object(transient.oid) is None
    finally:
        runtime.close()


def test_legacy_terminal_reconciliation_continues_after_pid_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime.open("local")
    try:
        pids: list[str] = []
        request_ids: list[str] = []
        for index in range(2):
            pid = runtime.process.spawn(image="base-agent:v0", goal=f"legacy pid {index}")
            runtime.capability.grant_once(
                pid,
                "human:owner",
                [CapabilityRight.WRITE],
                issued_by="test",
            )
            request_ids.append(runtime.human.ask(pid, f"legacy request {index}"))
            runtime.store.upsert_llm_pending_action(
                pid,
                {
                    "resume_token": f"legacy-starvation-{index}",
                    "wait_type": "human",
                    "request_id": request_ids[-1],
                    "filters": {},
                    "action": {"action": "ask_human"},
                    "data_flow_context": DataFlowContext().to_dict(),
                    "content_preview": "",
                    "tool_call_count": 1,
                    "status": "pending",
                },
            )
            pids.append(pid)
        runtime.store._execute(
            "UPDATE llm_pending_actions SET status = ?, updated_at = ? WHERE pid = ?",
            ("legacy_data_flow_invalidated", "0000", pids[0]),
        )
        runtime.store._execute(
            "UPDATE llm_pending_actions SET status = ?, updated_at = ? WHERE pid = ?",
            ("legacy_data_flow_invalidated", "9999", pids[1]),
        )
        original = runtime.process.reconcile_legacy_pending_action_terminal

        def fail_first(pid: str, **kwargs):
            if pid == pids[0]:
                raise RuntimeError("injected first pid cleanup failure")
            return original(pid, **kwargs)

        monkeypatch.setattr(
            runtime.process,
            "reconcile_legacy_pending_action_terminal",
            fail_first,
        )

        with pytest.raises(RuntimeError, match="injected first pid cleanup failure"):
            runtime._reconcile_legacy_pending_action_terminals()

        first = runtime.store.get_llm_pending_action(pids[0])
        second = runtime.store.get_llm_pending_action(pids[1])
        assert first is not None and first["status"] == "legacy_data_flow_invalidated"
        assert second is not None and second["status"] == "legacy_data_flow_reconciled"
        assert runtime.process.get(pids[1]).status == ProcessStatus.FAILED
        assert runtime.human.get(request_ids[1]).status == HumanRequestStatus.CANCELLED
    finally:
        runtime.close()


def test_failed_runtime_open_does_not_leak_object_task_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "failed-reconciliation.sqlite"
    runtime = Runtime.open(target)
    try:
        pid = runtime.process.spawn(image="base-agent:v0", goal="fail migration open")
        runtime.store.upsert_llm_pending_action(
            pid,
            {
                "resume_token": "failed-open-token",
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

    with sqlite3.connect(target) as connection:
        connection.execute(
            "UPDATE llm_pending_actions SET status = ? WHERE pid = ?",
            ("legacy_data_flow_invalidated", pid),
        )
        connection.commit()

    from agent_libos.runtime.process_manager import ProcessManager

    def fail_reconciliation(self, selected_pid: str, **_kwargs):
        raise RuntimeError(f"injected reconciliation failure for {selected_pid}")

    monkeypatch.setattr(
        ProcessManager,
        "reconcile_legacy_pending_action_terminal",
        fail_reconciliation,
    )
    before = sum(
        thread.name == "agent-libos-object-tasks"
        for thread in threading.enumerate()
    )

    with pytest.raises(RuntimeError, match="injected reconciliation failure"):
        Runtime.open(target)

    after = sum(
        thread.name == "agent-libos-object-tasks"
        for thread in threading.enumerate()
    )
    assert after == before


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
    monkeypatch: pytest.MonkeyPatch,
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
                connection.execute(
                    "DELETE FROM storage_migrations WHERE migration_name = ?",
                    ("llm_pending_action_data_flow_context",),
                )
                connection.commit()
        else:
            import psycopg

            with psycopg.connect(str(target), autocommit=True) as connection:
                connection.execute(
                    "UPDATE llm_pending_actions SET data_flow_context_json = %s WHERE pid = %s",
                    (raw_context, pid),
                )
                connection.execute(
                    "DELETE FROM storage_migrations WHERE migration_name = %s",
                    ("llm_pending_action_data_flow_context",),
                )

        reopened = Runtime.open(target, config=config)
        try:
            assert reopened.process.get(pid).status == ProcessStatus.FAILED
            pending = reopened.store.get_llm_pending_action(pid)
            assert pending is not None
            assert pending["status"] == "legacy_data_flow_reconciled"
            labels = DataFlowContext.from_dict(pending["data_flow_context"]).labels
            assert labels.sensitivity.value == "secret"
            assert labels.trust_level.value == "untrusted"
            assert labels.integrity.value == "untrusted"
        finally:
            reopened.close()

        import agent_libos.storage.sqlite as sqlite_store_module

        def fail_if_rescanned(_context):
            raise AssertionError("completed pending-context migration rescanned history")

        monkeypatch.setattr(
            sqlite_store_module,
            "_canonical_pending_data_flow_context",
            fail_if_rescanned,
        )
        second_reopen = Runtime.open(target, config=config)
        second_reopen.close()


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
                connection.execute(
                    "DELETE FROM storage_migrations WHERE migration_name = ?",
                    ("llm_pending_action_data_flow_context",),
                )
                connection.commit()
        else:
            import psycopg

            with psycopg.connect(str(target), autocommit=True) as connection:
                connection.execute(
                    "UPDATE llm_pending_actions SET data_flow_context_json = %s WHERE pid = %s",
                    (minimal, pid),
                )
                connection.execute(
                    "DELETE FROM storage_migrations WHERE migration_name = %s",
                    ("llm_pending_action_data_flow_context",),
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


@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_pending_context_migration_resumes_from_durable_cursor(
    kind: str,
    tmp_path: Path,
) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        assert target is not None
        runtime = Runtime.open(target, config=config)
        try:
            pids = [
                runtime.process.spawn(
                    image="base-agent:v0",
                    goal=f"resume pending-context migration {index}",
                )
                for index in range(3)
            ]
            for index, pid in enumerate(pids):
                runtime.store.upsert_llm_pending_action(
                    pid,
                    {
                        "resume_token": f"pending-migration-token-{index}",
                        "wait_type": "message",
                        "filters": {},
                        "action": {"action": "receive_process_messages"},
                        "data_flow_context": DataFlowContext().to_dict(),
                        "content_preview": "",
                        "tool_call_count": 1,
                        "status": "pending",
                    },
                )
            persisted = {
                row["pid"]: row["data_flow_context_json"]
                for row in runtime.store.select_table_rows("llm_pending_actions")
                if row["pid"] in set(pids)
            }
        finally:
            runtime.close()

        ordered_pids = sorted(pids)
        cursor = ordered_pids[0]
        partial_context = json.dumps({"labels": {"trust_level": "trusted"}})
        now = utc_now()
        if kind == "sqlite-file":
            with sqlite3.connect(target) as connection:
                for pid in ordered_pids:
                    connection.execute(
                        "UPDATE llm_pending_actions SET data_flow_context_json = ? WHERE pid = ?",
                        (partial_context, pid),
                    )
                connection.execute(
                    "UPDATE llm_pending_actions SET data_flow_context_json = ? WHERE pid = ?",
                    (persisted[cursor], cursor),
                )
                connection.execute(
                    "DELETE FROM storage_migrations WHERE migration_name = ?",
                    ("llm_pending_action_data_flow_context",),
                )
                connection.execute(
                    """
                    INSERT INTO storage_migrations (
                        migration_name, version, cursor_value, completed, updated_at
                    ) VALUES (?, ?, ?, 0, ?)
                    """,
                    ("llm_pending_action_data_flow_context", 1, cursor, now),
                )
                connection.commit()
        else:
            import psycopg

            with psycopg.connect(str(target), autocommit=True) as connection:
                for pid in ordered_pids:
                    connection.execute(
                        "UPDATE llm_pending_actions SET data_flow_context_json = %s WHERE pid = %s",
                        (partial_context, pid),
                    )
                connection.execute(
                    "UPDATE llm_pending_actions SET data_flow_context_json = %s WHERE pid = %s",
                    (persisted[cursor], cursor),
                )
                connection.execute(
                    "DELETE FROM storage_migrations WHERE migration_name = %s",
                    ("llm_pending_action_data_flow_context",),
                )
                connection.execute(
                    """
                    INSERT INTO storage_migrations (
                        migration_name, version, cursor_value, completed, updated_at
                    ) VALUES (%s, %s, %s, 0, %s)
                    """,
                    ("llm_pending_action_data_flow_context", 1, cursor, now),
                )

        reopened = Runtime.open(target, config=config)
        try:
            first = reopened.store.get_llm_pending_action(cursor)
            assert first is not None and first["status"] == "pending"
            assert reopened.process.get(cursor).status == ProcessStatus.RUNNABLE
            for pid in ordered_pids[1:]:
                pending = reopened.store.get_llm_pending_action(pid)
                assert pending is not None
                assert pending["status"] == "legacy_data_flow_reconciled"
                assert reopened.process.get(pid).status == ProcessStatus.FAILED
            migration = reopened.store.conn.execute(
                """
                SELECT completed, cursor_value
                  FROM storage_migrations
                 WHERE migration_name = ?
                """,
                ("llm_pending_action_data_flow_context",),
            ).fetchone()
            assert migration is not None
            assert bool(migration["completed"])
            assert migration["cursor_value"] is None
        finally:
            reopened.close()


@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_released_process_result_decodes_legacy_object_metadata(
    kind: str,
    tmp_path: Path,
) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        assert target is not None
        runtime = Runtime.open(target, config=config)
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="wait legacy result")
            child = runtime.process.spawn_child(parent, "produce legacy result")
            result = runtime.memory.create_object(
                child,
                ObjectType.SUMMARY,
                {"done": True},
                name="legacy.process.result",
            )
            runtime.process.exit(child, result=result)
        finally:
            runtime.close()

        legacy_metadata = json.dumps(
            {
                "title": "legacy result",
                "sensitivity": "historical-top-secret",
                "trust_level": "historical-trust",
                "integrity": "historical-integrity",
                "origin": 42,
            }
        )
        if kind == "sqlite-file":
            with sqlite3.connect(target) as connection:
                connection.execute(
                    "UPDATE objects SET metadata_json = ? WHERE oid = ?",
                    (legacy_metadata, result.oid),
                )
                connection.commit()
        else:
            import psycopg

            with psycopg.connect(str(target), autocommit=True) as connection:
                connection.execute(
                    "UPDATE objects SET metadata_json = %s WHERE oid = %s",
                    (legacy_metadata, result.oid),
                )

        reopened = Runtime.open(target, config=config)
        try:
            waited = reopened.process.wait(parent, child)
            assert waited.result is not None
            assert waited.result.oid == result.oid
            rows = reopened.store.select_table_rows("objects", "oid = ?", (result.oid,))
            assert rows[0]["lifecycle_state"] == "released"
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
def test_incomplete_legacy_message_labels_decode_conservatively(
    kind: str,
    tmp_path: Path,
) -> None:
    with _runtime_for_backend(kind, tmp_path) as runtime:
        pid = runtime.process.spawn(image="base-agent:v0", goal="decode partial legacy labels")
        message = runtime.messages.post(
            sender="legacy.sender",
            recipient_pid=pid,
            metadata={"custom": "preserved"},
        )
        runtime.store._execute(
            "UPDATE process_messages SET metadata_json = ? WHERE message_id = ?",
            (
                json.dumps(
                    {
                        "custom": "preserved",
                        "data_labels": {"trust_level": "trusted"},
                        "label_carrier_oid": "untrusted-legacy-carrier",
                    }
                ),
                message.message_id,
            ),
        )

        decoded = runtime.store.get_process_message(message.message_id)

        assert decoded is not None
        assert decoded.metadata["custom"] == "preserved"
        assert decoded.metadata["data_labels"]["sensitivity"] == "secret"
        assert decoded.metadata["data_labels"]["trust_level"] == "untrusted"
        assert decoded.metadata["data_labels"]["integrity"] == "untrusted"
        assert "label_carrier_oid" not in decoded.metadata


@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_legacy_message_metadata_migration_preserves_observation_cas(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        assert target is not None
        runtime = Runtime.open(target, config=config)
        try:
            pid = runtime.process.spawn(
                image="base-agent:v0",
                goal="observe migrated legacy message",
            )
            message = runtime.messages.post(
                sender="legacy.sender",
                recipient_pid=pid,
                metadata={"custom": "preserved"},
            )
        finally:
            runtime.close()

        legacy_metadata = json.dumps(
            {
                "custom": "preserved",
                "data_labels": {"trust_level": "trusted"},
                "label_carrier_oid": "untrusted-legacy-carrier",
            }
        )
        if kind == "sqlite-file":
            with sqlite3.connect(target) as connection:
                connection.execute(
                    "UPDATE process_messages SET metadata_json = ? WHERE message_id = ?",
                    (legacy_metadata, message.message_id),
                )
                connection.execute(
                    "DELETE FROM storage_migrations WHERE migration_name = ?",
                    ("process_message_metadata",),
                )
                connection.commit()
        else:
            import psycopg

            with psycopg.connect(str(target), autocommit=True) as connection:
                connection.execute(
                    "UPDATE process_messages SET metadata_json = %s WHERE message_id = %s",
                    (legacy_metadata, message.message_id),
                )
                connection.execute(
                    "DELETE FROM storage_migrations WHERE migration_name = %s",
                    ("process_message_metadata",),
                )

        reopened = Runtime.open(target, config=config)
        try:
            migrated = reopened.store.get_process_message(message.message_id)
            assert migrated is not None
            assert migrated.metadata["custom"] == "preserved"
            assert migrated.metadata["data_labels"]["sensitivity"] == "secret"
            assert "label_carrier_oid" not in migrated.metadata
            persisted = json.loads(
                reopened.store.select_table_rows(
                    "process_messages",
                    "message_id = ?",
                    (message.message_id,),
                )[0]["metadata_json"]
            )
            assert persisted == migrated.metadata

            observed = reopened.messages.observe_labels(pid, [migrated])

            assert len(observed) == 1
            stored = reopened.store.get_process_message(message.message_id)
            assert stored is not None
            assert stored.metadata["label_carrier_oid"] == observed[0]
            assert migrated.metadata["label_carrier_oid"] == observed[0]
        finally:
            reopened.close()

        import agent_libos.storage.sqlite as sqlite_store_module

        def fail_if_rescanned(_metadata):
            raise AssertionError("completed message metadata migration rescanned history")

        monkeypatch.setattr(
            sqlite_store_module,
            "_canonical_process_message_metadata",
            fail_if_rescanned,
        )
        second_reopen = Runtime.open(target, config=config)
        second_reopen.close()


@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_message_metadata_migration_resumes_from_durable_cursor(
    kind: str,
    tmp_path: Path,
) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        assert target is not None
        runtime = Runtime.open(target, config=config)
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="resume message migration")
            messages = [
                runtime.messages.post(
                    sender="legacy.sender",
                    recipient_pid=pid,
                    metadata={"custom": f"message-{index}"},
                )
                for index in range(3)
            ]
            persisted = {
                row["message_id"]: row["metadata_json"]
                for row in runtime.store.select_table_rows("process_messages")
                if row["message_id"] in {message.message_id for message in messages}
            }
        finally:
            runtime.close()

        ordered_ids = sorted(message.message_id for message in messages)
        cursor = ordered_ids[0]
        partial_by_id = {
            message_id: json.dumps(
                {
                    "custom": json.loads(persisted[message_id])["custom"],
                    "data_labels": {"trust_level": "trusted"},
                }
            )
            for message_id in ordered_ids
        }
        now = utc_now()
        if kind == "sqlite-file":
            with sqlite3.connect(target) as connection:
                for message_id in ordered_ids:
                    connection.execute(
                        "UPDATE process_messages SET metadata_json = ? WHERE message_id = ?",
                        (partial_by_id[message_id], message_id),
                    )
                connection.execute(
                    "UPDATE process_messages SET metadata_json = ? WHERE message_id = ?",
                    (persisted[cursor], cursor),
                )
                connection.execute(
                    "DELETE FROM storage_migrations WHERE migration_name = ?",
                    ("process_message_metadata",),
                )
                connection.execute(
                    """
                    INSERT INTO storage_migrations (
                        migration_name, version, cursor_value, completed, updated_at
                    ) VALUES (?, ?, ?, 0, ?)
                    """,
                    ("process_message_metadata", 1, cursor, now),
                )
                connection.commit()
        else:
            import psycopg

            with psycopg.connect(str(target), autocommit=True) as connection:
                for message_id in ordered_ids:
                    connection.execute(
                        "UPDATE process_messages SET metadata_json = %s WHERE message_id = %s",
                        (partial_by_id[message_id], message_id),
                    )
                connection.execute(
                    "UPDATE process_messages SET metadata_json = %s WHERE message_id = %s",
                    (persisted[cursor], cursor),
                )
                connection.execute(
                    "DELETE FROM storage_migrations WHERE migration_name = %s",
                    ("process_message_metadata",),
                )
                connection.execute(
                    """
                    INSERT INTO storage_migrations (
                        migration_name, version, cursor_value, completed, updated_at
                    ) VALUES (%s, %s, %s, 0, %s)
                    """,
                    ("process_message_metadata", 1, cursor, now),
                )

        reopened = Runtime.open(target, config=config)
        try:
            for message_id in ordered_ids:
                message = reopened.store.get_process_message(message_id)
                assert message is not None
                if message_id == cursor:
                    assert message.metadata == json.loads(persisted[cursor])
                else:
                    assert message.metadata["data_labels"]["sensitivity"] == "secret"
                    assert message.metadata["data_labels"]["trust_level"] == "untrusted"
            migration = reopened.store.conn.execute(
                "SELECT completed, cursor_value FROM storage_migrations WHERE migration_name = ?",
                ("process_message_metadata",),
            ).fetchone()
            assert migration is not None
            assert bool(migration["completed"])
            assert migration["cursor_value"] is None
        finally:
            reopened.close()


@pytest.mark.parametrize(
    "migration_name",
    [
        "llm_pending_action_data_flow_context",
        "process_message_metadata",
    ],
)
@pytest.mark.parametrize("kind", PERSISTENT_STORE_BACKENDS)
def test_future_storage_migration_version_fails_without_mutation(
    kind: str,
    migration_name: str,
    tmp_path: Path,
) -> None:
    with _persistent_target(kind, tmp_path) as (target, config):
        assert target is not None
        runtime = Runtime.open(target, config=config)
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="future migration version")
            message = runtime.messages.post(
                sender="future.sender",
                recipient_pid=pid,
                metadata={"custom": "current"},
            )
            runtime.store.upsert_llm_pending_action(
                pid,
                {
                    "resume_token": "future-migration-token",
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

        marker_cursor = "future-version-cursor"
        marker_updated_at = "future-version-marker"
        if migration_name == "llm_pending_action_data_flow_context":
            table = "llm_pending_actions"
            column = "data_flow_context_json"
            key_column = "pid"
            key = pid
            future_value = json.dumps(
                {"labels": {"trust_level": "trusted"}, "future_field": True}
            )
        else:
            table = "process_messages"
            column = "metadata_json"
            key_column = "message_id"
            key = message.message_id
            future_value = json.dumps(
                {
                    "custom": "future",
                    "data_labels": {"trust_level": "trusted"},
                    "future_field": True,
                }
            )

        if kind == "sqlite-file":
            with sqlite3.connect(target) as connection:
                connection.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {key_column} = ?",
                    (future_value, key),
                )
                connection.execute(
                    """
                    UPDATE storage_migrations
                       SET version = 2, cursor_value = ?, completed = 1, updated_at = ?
                     WHERE migration_name = ?
                    """,
                    (marker_cursor, marker_updated_at, migration_name),
                )
                connection.commit()
        else:
            import psycopg

            with psycopg.connect(str(target), autocommit=True) as connection:
                connection.execute(
                    f"UPDATE {table} SET {column} = %s WHERE {key_column} = %s",
                    (future_value, key),
                )
                connection.execute(
                    """
                    UPDATE storage_migrations
                       SET version = 2, cursor_value = %s, completed = 1, updated_at = %s
                     WHERE migration_name = %s
                    """,
                    (marker_cursor, marker_updated_at, migration_name),
                )

        with pytest.raises(
            ValidationError,
            match=rf"storage migration {migration_name} version 2 is newer",
        ):
            Runtime.open(target, config=config)

        if kind == "sqlite-file":
            with sqlite3.connect(target) as connection:
                persisted_value = connection.execute(
                    f"SELECT {column} FROM {table} WHERE {key_column} = ?",
                    (key,),
                ).fetchone()
                marker = connection.execute(
                    """
                    SELECT version, cursor_value, completed, updated_at
                      FROM storage_migrations
                     WHERE migration_name = ?
                    """,
                    (migration_name,),
                ).fetchone()
        else:
            import psycopg

            with psycopg.connect(str(target), autocommit=True) as connection:
                persisted_value = connection.execute(
                    f"SELECT {column} FROM {table} WHERE {key_column} = %s",
                    (key,),
                ).fetchone()
                marker = connection.execute(
                    """
                    SELECT version, cursor_value, completed, updated_at
                      FROM storage_migrations
                     WHERE migration_name = %s
                    """,
                    (migration_name,),
                ).fetchone()

        assert persisted_value == (future_value,)
        assert marker == (2, marker_cursor, 1, marker_updated_at)


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


def test_sqlite_gui_snapshot_visibility_legacy_backfill_is_resumable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-gui-visibility.sqlite"
    base_config = AgentLibOSConfig()
    config = replace(
        base_config,
        gui=replace(base_config.gui, snapshot_collection_max_items=2),
    )
    runtime = Runtime.open(database, config=config)
    try:
        visible_event = runtime.events.emit(
            EventType.PROCESS_CREATED,
            source="test",
            payload={"marker": "visible-event"},
        )
        hidden_event = runtime.events.emit(
            EventType.HUMAN_OUTPUT,
            source="test",
            payload={"purpose": "gui_presentation"},
        )
        visible_audit = runtime.audit.record(
            actor="test",
            action="test.visible",
            decision={"marker": "visible-audit"},
        )
        hidden_audit = runtime.audit.record(
            actor="test",
            action="human.output",
            decision={"purpose": "gui_presentation"},
        )
    finally:
        runtime.close()

    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX IF EXISTS idx_events_gui_snapshot_visible_created")
        connection.execute("DROP INDEX IF EXISTS idx_audit_gui_snapshot_visible_created")
        connection.execute("ALTER TABLE events DROP COLUMN gui_snapshot_visible")
        connection.execute("ALTER TABLE audit_records DROP COLUMN gui_snapshot_visible")
        # Simulate an interrupted migration that added the nullable sentinel
        # but did not complete its data backfill or index creation.
        connection.execute("ALTER TABLE events ADD COLUMN gui_snapshot_visible INTEGER")
        connection.execute("ALTER TABLE audit_records ADD COLUMN gui_snapshot_visible INTEGER")
        connection.commit()

    reopened = Runtime.open(database, config=config)
    try:
        event_flags = {
            row["event_id"]: row["gui_snapshot_visible"]
            for row in reopened.store.select_table_rows("events")
        }
        audit_flags = {
            row["record_id"]: row["gui_snapshot_visible"]
            for row in reopened.store.select_table_rows("audit_records")
        }

        assert event_flags[visible_event.event_id] == 1
        assert event_flags[hidden_event.event_id] == 0
        assert audit_flags[visible_audit.record_id] == 1
        assert audit_flags[hidden_audit.record_id] == 0
        assert None not in event_flags.values()
        assert None not in audit_flags.values()
        visible_event_ids = {
            item.event_id
            for item in reopened.store.list_events(include_gui_presentation=False)
        }
        visible_audit_ids = {
            item.record_id
            for item in reopened.store.list_audit(include_gui_presentation=False)
        }
        assert visible_event.event_id in visible_event_ids
        assert hidden_event.event_id not in visible_event_ids
        assert visible_audit.record_id in visible_audit_ids
        assert hidden_audit.record_id not in visible_audit_ids

        event_plan = "\n".join(
            str(row[3])
            for row in reopened.store.conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM events "
                "WHERE gui_snapshot_visible = 1 "
                "ORDER BY created_at DESC, event_id DESC LIMIT ?",
                (2,),
            )
        )
        audit_plan = "\n".join(
            str(row[3])
            for row in reopened.store.conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM audit_records "
                "WHERE gui_snapshot_visible = 1 "
                "ORDER BY timestamp DESC, record_id DESC LIMIT ?",
                (2,),
            )
        )
        assert "idx_events_gui_snapshot_visible_created" in event_plan
        assert "idx_audit_gui_snapshot_visible_created" in audit_plan
    finally:
        reopened.close()


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

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_libos.config import AgentLibOSConfig, RuntimeDefaults
from agent_libos.models import (
    CheckpointPayloadDeliveryAttemptState,
    ObjectMetadata,
    ObjectType,
    OperationOutcome,
    OperationState,
)
from agent_libos.models.exceptions import ValidationError
from agent_libos.modules.registry import RuntimeModuleRegistry
from agent_libos.runtime.checkpoint_manager import CheckpointManager
from agent_libos.runtime.checkpoint_reconciliation import CheckpointRestoreReconciler
from agent_libos.runtime.lifecycle import RuntimeLifecycle
from agent_libos.runtime.operation_manager import OperationManager
from agent_libos.runtime.runtime import Runtime
from agent_libos.storage import SQLiteStore
from agent_libos.storage.repositories import CheckpointRestorePublicationWriter
from agent_libos.utils.ids import utc_now
from agent_libos.utils.serde import dumps


PAGE_SIZE = 2


class _OneShotCommitFaultConnection:
    """Raise once either immediately before or immediately after real commit."""

    def __init__(self, connection: Any, *, delegate_first: bool) -> None:
        self._connection = connection
        self._delegate_first = delegate_first
        self._fault_pending = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def commit(self) -> None:
        if not self._fault_pending:
            self._connection.commit()
            return
        self._fault_pending = False
        if self._delegate_first:
            self._connection.commit()
        raise RuntimeError("injected payload acknowledgement commit fault")


def _install_ack_commit_fault(
    monkeypatch: pytest.MonkeyPatch,
    *,
    delegate_first: bool,
) -> list[Any]:
    original_ack = (
        CheckpointRestorePublicationWriter.ack_checkpoint_payload_delivery_attempt
    )
    attempts: list[Any] = []
    armed = False

    def ack_then_fault_outer_commit(
        writer: CheckpointRestorePublicationWriter,
        attempt: Any,
    ) -> bool:
        nonlocal armed
        acknowledged = original_ack(writer, attempt)
        assert acknowledged is True
        attempts.append(attempt)
        if not armed:
            backend = writer._CheckpointRestorePublicationWriter__backend
            backend.conn = _OneShotCommitFaultConnection(
                backend.conn,
                delegate_first=delegate_first,
            )
            armed = True
        return acknowledged

    monkeypatch.setattr(
        CheckpointRestorePublicationWriter,
        "ack_checkpoint_payload_delivery_attempt",
        ack_then_fault_outer_commit,
    )
    return attempts


def _observe_failed_open_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, bool]]:
    original_open_scope = RuntimeLifecycle.open_on_next_commit
    failed_scope_states: list[tuple[str, bool]] = []

    @contextmanager
    def observe_open_scope(lifecycle: RuntimeLifecycle) -> Iterator[None]:
        try:
            with original_open_scope(lifecycle):
                yield
        except BaseException:
            failed_scope_states.append(
                (lifecycle.state, lifecycle._ever_opened)
            )
            raise

    monkeypatch.setattr(
        RuntimeLifecycle,
        "open_on_next_commit",
        observe_open_scope,
    )
    return failed_scope_states


def _config() -> AgentLibOSConfig:
    return AgentLibOSConfig(
        runtime=RuntimeDefaults(publication_reconciliation_page_size=PAGE_SIZE)
    )


def _seed_pending_payload_backlog(
    target: Path,
    *,
    total: int,
    config: AgentLibOSConfig,
) -> tuple[list[str], list[str], str]:
    """Create real restore plans/operations, then expose their recovery payload intent."""

    runtime = Runtime.open(target, config=config)
    publication_ids: list[str] = []
    operation_ids: list[str] = []
    object_oid = ""
    try:
        pid = runtime.process.spawn(goal="checkpoint payload delivery handshake")
        handle = runtime.memory.create_object(
            pid,
            ObjectType.SUMMARY,
            {"version": 1},
            ObjectMetadata(title="payload handshake state"),
            immutable=False,
            name="checkpoint.payload.handshake.state",
        )
        object_oid = handle.oid
        checkpoint_id = runtime.checkpoint.create(
            pid,
            "payload handshake fixture",
            actor=pid,
            require_capability=False,
        )
        for _index in range(total):
            result = runtime.checkpoint.restore(
                "test",
                checkpoint_id,
                require_capability=False,
            )
            publication_id = str(result["publication_id"])
            publication = runtime.store.get_runtime_publication(publication_id)
            assert publication is not None
            assert publication["state"] == "committed"
            assert publication["phase"] == "reconciled"
            assert publication["operation_reconciled"] is True
            assert publication["payload_delivery_state"] is None
            publication_ids.append(publication_id)
            operation_ids.append(str(publication["plan"]["operation_id"]))

        # Online restore does not need a startup payload handoff. These rows are
        # otherwise complete, real checkpoint publications; adding the exact
        # pending projection models the durable state produced by recovery.
        shared_created_at = utc_now()
        with runtime.store.transaction() as cursor:
            for publication_id in publication_ids:
                publication = runtime.store.get_runtime_publication(publication_id)
                assert publication is not None
                receipt = dict(publication["receipt"])
                receipt["payload_delivery"] = {"state": "pending"}
                updated = cursor.execute(
                    "UPDATE runtime_publications "
                    "SET receipt_json = ?, payload_delivery_state = 'pending', "
                    "payload_delivery_attempt_id = NULL, "
                    "payload_delivery_started_at = NULL, created_at = ?, updated_at = ? "
                    "WHERE publication_id = ? AND payload_delivery_state IS NULL",
                    (
                        dumps(receipt),
                        shared_created_at,
                        shared_created_at,
                        publication_id,
                    ),
                )
                assert updated.rowcount == 1
        for publication_id in publication_ids:
            publication = runtime.store.get_runtime_publication(publication_id)
            assert publication is not None
            assert publication["receipt"]["payload_delivery"] == {
                "state": "pending"
            }
            assert publication["payload_delivery_state"] == "pending"
        return publication_ids, operation_ids, object_oid
    finally:
        runtime.close()


def _durable_handshake_state(
    target: Path,
    *,
    config: AgentLibOSConfig,
    publication_ids: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    store = SQLiteStore(target, config=config)
    try:
        publications = []
        for publication_id in publication_ids:
            publication = store.get_runtime_publication(publication_id)
            assert publication is not None
            publications.append(dict(publication))
        attempts = store.select_table_rows(
            "checkpoint_payload_delivery_attempts",
            order_by="started_at, attempt_id",
        )
        return publications, attempts
    finally:
        store.close()


def _assert_completed_delivery(
    runtime: Runtime,
    publication_ids: list[str],
) -> None:
    for publication_id in publication_ids:
        publication = runtime.store.get_runtime_publication(publication_id)
        assert publication is not None
        assert publication["payload_delivery_state"] == "completed"
        assert publication["payload_delivery_attempt_id"]
        assert publication["payload_delivery_started_at"]
        assert publication["receipt"]["payload_delivery"] == {
            "state": "completed"
        }


def _dirty_bound_operation(store: Any, operation_id: str) -> None:
    operation = store.get_operation(operation_id)
    assert operation is not None
    assert store.update_operation(
        replace(
            operation,
            state=OperationState.RUNNING,
            outcome=OperationOutcome.PENDING,
            completed_at=None,
            updated_at=utc_now(),
        ),
        expected_states=[OperationState.TERMINAL.value],
    )


def test_payload_handshake_pages_a_multi_page_backlog_with_bounded_transactions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    total = 2 * PAGE_SIZE + 1
    target = tmp_path / "payload-handshake-pages.sqlite"
    publication_ids, _operation_ids, _object_oid = _seed_pending_payload_backlog(
        target,
        total=total,
        config=config,
    )

    original_query = SQLiteStore._query
    original_execute = SQLiteStore._execute
    original_transaction = SQLiteStore.transaction
    page_rows: dict[str, list[int]] = {}
    payload_writes_by_outer_transaction: list[int] = []
    active_payload_writes: dict[int, int] = {}

    def tracked_query(
        store: SQLiteStore,
        sql: str,
        params: Any = (),
    ) -> list[Any]:
        rows = original_query(store, sql, params)
        if "/* checkpoint-payload-delivery */" in sql:
            state = str(params[0])
            page_rows.setdefault(state, []).append(len(rows))
        return rows

    def tracked_execute(
        store: SQLiteStore,
        sql: str,
        params: Any = (),
    ) -> Any:
        if (
            id(store) in active_payload_writes
            and "UPDATE runtime_publications SET receipt_json" in sql
            and "payload_delivery_state" in sql
        ):
            active_payload_writes[id(store)] += 1
        return original_execute(store, sql, params)

    @contextmanager
    def tracked_transaction(
        store: SQLiteStore,
        *,
        include_object_payloads: bool = False,
    ) -> Iterator[Any]:
        outer = store._transaction_depth == 0
        if outer:
            active_payload_writes[id(store)] = 0
        try:
            with original_transaction(
                store,
                include_object_payloads=include_object_payloads,
            ) as cursor:
                yield cursor
        finally:
            if outer:
                writes = active_payload_writes.pop(id(store))
                if writes:
                    payload_writes_by_outer_transaction.append(writes)

    monkeypatch.setattr(SQLiteStore, "_query", tracked_query)
    monkeypatch.setattr(SQLiteStore, "_execute", tracked_execute)
    monkeypatch.setattr(SQLiteStore, "transaction", tracked_transaction)

    reopened = Runtime.open(target, config=config)
    try:
        _assert_completed_delivery(reopened, publication_ids)
        reconciler = reopened.checkpoint._restore_reconciler
        assert not hasattr(reconciler, "_payload_hydrated_publications")
        assert not hasattr(reconciler, "_pending_payload_delivery_ids")

        assert page_rows["confirmed"] == [PAGE_SIZE + 1, PAGE_SIZE + 1, 1]
        assert max(
            row_count
            for state_rows in page_rows.values()
            for row_count in state_rows
        ) <= PAGE_SIZE + 1
        assert len(payload_writes_by_outer_transaction) == 9
        assert sum(payload_writes_by_outer_transaction) == 3 * total
        assert max(payload_writes_by_outer_transaction) == PAGE_SIZE
    finally:
        reopened.close()


def test_false_ack_cas_never_opens_and_is_compensated_before_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    target = tmp_path / "payload-handshake-false-ack.sqlite"
    publication_ids, _operation_ids, _object_oid = _seed_pending_payload_backlog(
        target,
        total=1,
        config=config,
    )

    original_ack = (
        CheckpointRestorePublicationWriter.ack_checkpoint_payload_delivery_attempt
    )
    original_open_scope = RuntimeLifecycle.open_on_next_commit
    ack_calls = 0
    failed_scope_states: list[tuple[str, bool]] = []

    def reject_first_ack(
        writer: CheckpointRestorePublicationWriter,
        attempt: Any,
    ) -> bool:
        nonlocal ack_calls
        ack_calls += 1
        if ack_calls == 1:
            return False
        return original_ack(writer, attempt)

    @contextmanager
    def observe_open_scope(lifecycle: RuntimeLifecycle) -> Iterator[None]:
        try:
            with original_open_scope(lifecycle):
                yield
        except BaseException:
            failed_scope_states.append(
                (lifecycle.state, lifecycle._ever_opened)
            )
            raise

    monkeypatch.setattr(
        CheckpointRestorePublicationWriter,
        "ack_checkpoint_payload_delivery_attempt",
        reject_first_ack,
    )
    monkeypatch.setattr(
        RuntimeLifecycle,
        "open_on_next_commit",
        observe_open_scope,
    )

    with pytest.raises(
        ValidationError,
        match="payload delivery attempt acknowledgement failed",
    ):
        Runtime.open(target, config=config)

    assert failed_scope_states == [("starting", False)]
    publications, attempts = _durable_handshake_state(
        target,
        config=config,
        publication_ids=publication_ids,
    )
    assert [item["payload_delivery_state"] for item in publications] == [
        "pending"
    ]
    assert [item["payload_delivery_attempt_id"] for item in publications] == [
        None
    ]
    assert [item["state"] for item in attempts] == ["aborted"]
    assert all(item["acked_at"] is None for item in attempts)

    reopened = Runtime.open(target, config=config)
    try:
        assert reopened.lifecycle.state == "open"
        assert ack_calls == 2
        _assert_completed_delivery(reopened, publication_ids)
        attempt_rows = reopened.store.select_table_rows(
            "checkpoint_payload_delivery_attempts"
        )
        assert sorted(item["state"] for item in attempt_rows) == [
            "aborted",
            "acked",
        ]
    finally:
        reopened.close()


def test_postcommit_driver_error_confirms_acked_attempt_and_opens_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    target = tmp_path / "payload-handshake-postcommit-confirmed.sqlite"
    publication_ids, _operation_ids, object_oid = _seed_pending_payload_backlog(
        target,
        total=PAGE_SIZE + 1,
        config=config,
    )

    failed_scope_states = _observe_failed_open_scope(monkeypatch)
    original_mark_open = RuntimeLifecycle.mark_open
    mark_open_entry_states: list[str] = []

    def observe_mark_open(lifecycle: RuntimeLifecycle) -> None:
        mark_open_entry_states.append(lifecycle.state)
        original_mark_open(lifecycle)

    monkeypatch.setattr(RuntimeLifecycle, "mark_open", observe_mark_open)
    attempts = _install_ack_commit_fault(
        monkeypatch,
        delegate_first=True,
    )

    reopened = Runtime.open(target, config=config)
    try:
        # The lifecycle OPEN inside the commit guard was rolled back in memory.
        # Exact typed readback then proved the ACK commit happened and allowed
        # one pure STARTING -> OPEN transition without replaying the payload.
        assert failed_scope_states == [("starting", False)]
        assert mark_open_entry_states == ["starting", "starting"]
        assert reopened.lifecycle.state == "open"
        assert len(attempts) == 1
        assert reopened.checkpoint._get_startup_payload_delivery_attempt_state(
            attempts[0]
        ) is CheckpointPayloadDeliveryAttemptState.ACKED
        _assert_completed_delivery(reopened, publication_ids)
        assert reopened.store.object_payload(object_oid) == {"version": 1}
        assert not reopened.store.is_recovered_object_payload(object_oid)
        attempt_rows = reopened.store.select_table_rows(
            "checkpoint_payload_delivery_attempts"
        )
        assert [item["state"] for item in attempt_rows] == ["acked"]
    finally:
        reopened.close()


def test_precommit_ack_failure_stays_starting_and_pages_compensation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    total = 2 * PAGE_SIZE + 1
    target = tmp_path / "payload-handshake-precommit-failure.sqlite"
    publication_ids, _operation_ids, _object_oid = _seed_pending_payload_backlog(
        target,
        total=total,
        config=config,
    )

    original_query = SQLiteStore._query
    compensation_page_rows: list[int] = []

    def tracked_query(
        store: SQLiteStore,
        sql: str,
        params: Any = (),
    ) -> list[Any]:
        rows = original_query(store, sql, params)
        if (
            "/* checkpoint-payload-delivery */" in sql
            and params
            and params[0] == "completed"
        ):
            compensation_page_rows.append(len(rows))
        return rows

    monkeypatch.setattr(SQLiteStore, "_query", tracked_query)
    failed_scope_states = _observe_failed_open_scope(monkeypatch)
    attempts = _install_ack_commit_fault(
        monkeypatch,
        delegate_first=False,
    )

    with pytest.raises(
        RuntimeError,
        match="injected payload acknowledgement commit fault",
    ):
        Runtime.open(target, config=config)

    assert failed_scope_states == [("starting", False)]
    assert len(attempts) == 1
    assert compensation_page_rows == [PAGE_SIZE + 1, PAGE_SIZE + 1, 1]
    publications, attempt_rows = _durable_handshake_state(
        target,
        config=config,
        publication_ids=publication_ids,
    )
    assert {item["payload_delivery_state"] for item in publications} == {
        "pending"
    }
    assert {item["payload_delivery_attempt_id"] for item in publications} == {
        None
    }
    assert [item["state"] for item in attempt_rows] == ["aborted"]
    assert attempt_rows[0]["acked_at"] is None

    reopened = Runtime.open(target, config=config)
    try:
        assert reopened.lifecycle.state == "open"
        _assert_completed_delivery(reopened, publication_ids)
        assert sorted(
            item["state"]
            for item in reopened.store.select_table_rows(
                "checkpoint_payload_delivery_attempts"
            )
        ) == ["aborted", "acked"]
    finally:
        reopened.close()


@pytest.mark.parametrize("confirmation_fault", ("mismatch", "read_failure"))
def test_unconfirmed_postcommit_ack_never_mutates_publication_rows(
    confirmation_fault: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    target = tmp_path / f"payload-handshake-{confirmation_fault}.sqlite"
    publication_ids, _operation_ids, _object_oid = _seed_pending_payload_backlog(
        target,
        total=PAGE_SIZE + 1,
        config=config,
    )

    original_transition = CheckpointRestorePublicationWriter.transition_payload_delivery
    original_attempt_state = (
        CheckpointManager._get_startup_payload_delivery_attempt_state
    )
    confirmation_calls = 0
    compensation_calls: list[tuple[str | None, str]] = []
    snapshot_at_confirmation: list[dict[str, Any]] = []

    def observe_compensation(
        writer: CheckpointRestorePublicationWriter,
        publication_id: str,
        **kwargs: Any,
    ) -> bool:
        if (
            kwargs.get("expected_delivery_state") in {"confirmed", "completed"}
            and kwargs.get("delivery_state") == "pending"
        ):
            compensation_calls.append(
                (
                    kwargs.get("expected_delivery_state"),
                    publication_id,
                )
            )
        return original_transition(writer, publication_id, **kwargs)

    def fail_exact_attempt_confirmation(
        manager: CheckpointManager,
        attempt: Any,
    ) -> CheckpointPayloadDeliveryAttemptState | None:
        nonlocal confirmation_calls
        confirmation_calls += 1
        for publication_id in publication_ids:
            publication = manager._restore_reconciler._store.get_runtime_publication(
                publication_id
            )
            assert publication is not None
            snapshot_at_confirmation.append(dict(publication))
        if confirmation_fault == "read_failure":
            raise RuntimeError("injected exact attempt confirmation failure")
        durable = original_attempt_state(manager, attempt)
        assert durable is CheckpointPayloadDeliveryAttemptState.ACKED
        return CheckpointPayloadDeliveryAttemptState.ABORTED

    monkeypatch.setattr(
        CheckpointRestorePublicationWriter,
        "transition_payload_delivery",
        observe_compensation,
    )
    monkeypatch.setattr(
        CheckpointManager,
        "_get_startup_payload_delivery_attempt_state",
        fail_exact_attempt_confirmation,
    )
    _install_ack_commit_fault(
        monkeypatch,
        delegate_first=True,
    )

    with pytest.raises(Exception):
        Runtime.open(target, config=config)

    assert confirmation_calls == 1
    assert len(snapshot_at_confirmation) == len(publication_ids)
    assert compensation_calls == []
    publications, attempt_rows = _durable_handshake_state(
        target,
        config=config,
        publication_ids=publication_ids,
    )
    assert publications == snapshot_at_confirmation
    assert {item["payload_delivery_state"] for item in publications} == {
        "completed"
    }
    assert [item["state"] for item in attempt_rows] == ["acked"]


@pytest.mark.parametrize(
    "failure_stage",
    ("prepare", "complete", "terminal_repair"),
)
def test_partial_payload_handshake_stage_failure_recovers_on_next_restart(
    failure_stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    total = PAGE_SIZE + 1
    target = tmp_path / f"payload-handshake-{failure_stage}.sqlite"
    publication_ids, operation_ids, _object_oid = _seed_pending_payload_backlog(
        target,
        total=total,
        config=config,
    )

    original_transition = CheckpointRestorePublicationWriter.transition_payload_delivery
    original_reconcile = CheckpointRestoreReconciler._reconcile_operation
    original_hooks = RuntimeModuleRegistry._run_startup_hooks_locked
    stage_calls = 0
    injected = False
    hook_calls = 0

    def transition_with_one_page_committed(
        writer: CheckpointRestorePublicationWriter,
        publication_id: str,
        **kwargs: Any,
    ) -> bool:
        nonlocal stage_calls, injected
        selected_transition = (
            kwargs.get("expected_delivery_state"),
            kwargs.get("delivery_state"),
        )
        target_transition = {
            "prepare": ("pending", "confirmed"),
            "complete": ("confirmed", "completed"),
        }.get(failure_stage)
        if (
            target_transition is not None
            and selected_transition == target_transition
            and not injected
        ):
            stage_calls += 1
            if stage_calls == PAGE_SIZE + 1:
                injected = True
                raise RuntimeError(f"injected {failure_stage} page failure")
        return original_transition(writer, publication_id, **kwargs)

    def dirty_operations_after_first_startup_hook(
        registry: RuntimeModuleRegistry,
    ) -> None:
        nonlocal hook_calls
        original_hooks(registry)
        hook_calls += 1
        if failure_stage != "terminal_repair" or hook_calls != 1:
            return
        store = registry._hook_services.store
        for operation_id in operation_ids:
            _dirty_bound_operation(store, operation_id)
        for publication_id in publication_ids:
            publication = store.get_runtime_publication(publication_id)
            assert publication is not None
            assert publication["operation_reconciled"] is False

    def reconcile_with_one_page_committed(
        reconciler: CheckpointRestoreReconciler,
        publication: Any,
        outcome: OperationOutcome,
    ) -> None:
        nonlocal stage_calls, injected
        if (
            failure_stage == "terminal_repair"
            and publication["payload_delivery_state"] == "completed"
            and not injected
        ):
            stage_calls += 1
            if stage_calls == PAGE_SIZE + 1:
                injected = True
                raise RuntimeError("injected terminal_repair page failure")
        original_reconcile(reconciler, publication, outcome)

    monkeypatch.setattr(
        CheckpointRestorePublicationWriter,
        "transition_payload_delivery",
        transition_with_one_page_committed,
    )
    monkeypatch.setattr(
        RuntimeModuleRegistry,
        "_run_startup_hooks_locked",
        dirty_operations_after_first_startup_hook,
    )
    monkeypatch.setattr(
        CheckpointRestoreReconciler,
        "_reconcile_operation",
        reconcile_with_one_page_committed,
    )

    with pytest.raises(
        RuntimeError,
        match=f"injected {failure_stage} page failure",
    ):
        Runtime.open(target, config=config)

    assert injected is True
    assert stage_calls == PAGE_SIZE + 1
    publications, attempts = _durable_handshake_state(
        target,
        config=config,
        publication_ids=publication_ids,
    )
    assert {item["payload_delivery_state"] for item in publications} == {
        "pending"
    }
    assert {item["payload_delivery_attempt_id"] for item in publications} == {
        None
    }
    assert [item["state"] for item in attempts] == ["aborted"]
    if failure_stage == "terminal_repair":
        assert sum(item["operation_reconciled"] for item in publications) == PAGE_SIZE

    reopened = Runtime.open(target, config=config)
    try:
        assert reopened.lifecycle.state == "open"
        _assert_completed_delivery(reopened, publication_ids)
        for publication_id, operation_id in zip(
            publication_ids,
            operation_ids,
            strict=True,
        ):
            publication = reopened.store.get_runtime_publication(publication_id)
            operation = reopened.store.get_operation(operation_id)
            assert publication is not None
            assert publication["operation_reconciled"] is True
            assert operation is not None
            assert operation.state == OperationState.TERMINAL
            assert operation.outcome == OperationOutcome.SUCCEEDED
        assert not any(
            item["state"] == "preparing"
            for item in reopened.store.select_table_rows(
                "checkpoint_payload_delivery_attempts"
            )
        )
    finally:
        reopened.close()


def test_pending_checkpoint_operation_is_repaired_before_generic_stale_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    target = tmp_path / "payload-handshake-operation-order.sqlite"
    publication_ids, operation_ids, _object_oid = _seed_pending_payload_backlog(
        target,
        total=1,
        config=config,
    )
    publication_id = publication_ids[0]
    operation_id = operation_ids[0]

    store = SQLiteStore(target, config=config)
    try:
        _dirty_bound_operation(store, operation_id)
        publication = store.get_runtime_publication(publication_id)
        operation = store.get_operation(operation_id)
        assert publication is not None
        assert publication["payload_delivery_state"] == "pending"
        assert publication["operation_reconciled"] is False
        assert operation is not None
        assert operation.state == OperationState.RUNNING
        assert operation.outcome == OperationOutcome.PENDING
    finally:
        store.close()

    original_interrupt = OperationManager.interrupt_stale_running
    observed_before_generic_recovery: list[
        tuple[str, bool, OperationState, OperationOutcome]
    ] = []

    def observe_then_interrupt(manager: OperationManager) -> Any:
        publication = manager.publications.get_runtime_publication(publication_id)
        operation = manager.get_operation(operation_id)
        assert publication is not None
        assert operation is not None
        observed_before_generic_recovery.append(
            (
                str(publication["payload_delivery_state"]),
                bool(publication["operation_reconciled"]),
                operation.state,
                operation.outcome,
            )
        )
        return original_interrupt(manager)

    monkeypatch.setattr(
        OperationManager,
        "interrupt_stale_running",
        observe_then_interrupt,
    )

    reopened = Runtime.open(target, config=config)
    try:
        assert observed_before_generic_recovery == [
            (
                "pending",
                True,
                OperationState.TERMINAL,
                OperationOutcome.SUCCEEDED,
            )
        ]
        _assert_completed_delivery(reopened, publication_ids)
        operation = reopened.store.get_operation(operation_id)
        assert operation is not None
        assert operation.state == OperationState.TERMINAL
        assert operation.outcome == OperationOutcome.SUCCEEDED
    finally:
        reopened.close()

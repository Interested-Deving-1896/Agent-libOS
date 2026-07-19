from __future__ import annotations

import contextlib
import math
import os
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from agent_libos.models import ProcessRestoreEpoch
from agent_libos.models.exceptions import ValidationError
from agent_libos.models.snapshot import SnapshotRows
from agent_libos.storage import PostgresStore, SQLiteStore
from agent_libos.storage.repositories import SnapshotCheckpointRepository


STORE_BACKENDS = [
    "sqlite",
    pytest.param("postgres", marks=pytest.mark.postgres),
]


class _BoundedCursor:
    def __init__(self, cursor: Any, parameter_counts: list[int]) -> None:
        self._cursor = cursor
        self._parameter_counts = parameter_counts

    def execute(self, sql: str, params: Iterable[Any] = ()) -> Any:
        selected = tuple(params)
        self._parameter_counts.append(len(selected))
        assert len(selected) <= 999
        return self._cursor.execute(sql, selected)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


def _restore_row(pid: str) -> dict[str, Any]:
    return {
        "pid": pid,
        "revision": 0,
        "execution_generation": 0,
        "state_generation": 0,
    }


def test_checkpoint_restore_preparation_batches_above_sqlite_variable_limit(
) -> None:
    store = SQLiteStore(":memory:")
    repository = SnapshotCheckpointRepository(store)
    try:
        statements: list[str] = []
        store.conn.set_trace_callback(statements.append)
        rows = SnapshotRows(
            processes=tuple(
                _restore_row(f"pid_restore_{index:05d}")
                for index in range(32_767)
            )
        )

        restored = repository.prepare_checkpoint_restore_process_rows(
            rows,
            restored_capability_rows=(),
        )

        assert len(restored) == 32_767
        assert restored[0]["revision"] == 1
        assert restored[-1]["execution_generation"] == 1
        assert restored[-1]["state_generation"] == 1
        epoch_statements = [
            statement
            for statement in statements
            if "runtime_counters" in statement
        ]
        # Three counter statements per 150-PID bulk page: floor UPSERT,
        # increment, and exact readback. This guards against restoring the old
        # eight round trips per process while exercising the real backend.
        expected_pages = math.ceil(len(rows.processes) / 150)
        assert len(epoch_statements) == expected_pages * 3
        assert len(statements) < 800
    finally:
        store.conn.set_trace_callback(None)
        store.close()


@pytest.mark.parametrize("backend", STORE_BACKENDS)
def test_snapshot_scope_queries_and_deletes_batch_below_legacy_bind_limit(
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _store_for_backend(backend) as store:
        repository = SnapshotCheckpointRepository(store)
        parameter_counts: list[int] = []
        original_select = store.select_table_rows
        original_transaction = repository.transaction

        def bounded_select(
            table: str,
            where_sql: str = "",
            params: Iterable[Any] = (),
            *,
            order_by: str | None = None,
        ) -> list[dict[str, Any]]:
            selected = tuple(params)
            parameter_counts.append(len(selected))
            assert len(selected) <= 999
            return original_select(
                table,
                where_sql,
                selected,
                order_by=order_by,
            )

        @contextlib.contextmanager
        def bounded_transaction(
            *,
            include_object_payloads: bool = False,
        ) -> Iterator[_BoundedCursor]:
            with original_transaction(
                include_object_payloads=include_object_payloads
            ) as cursor:
                yield _BoundedCursor(cursor, parameter_counts)

        monkeypatch.setattr(store, "select_table_rows", bounded_select)
        monkeypatch.setattr(repository, "transaction", bounded_transaction)
        values = [f"scope_{index:04d}" for index in range(1_205)]
        present_namespaces = [values[1_004], values[2], values[703]]
        with store.transaction() as cursor:
            cursor.executemany(
                "INSERT INTO object_namespaces "
                "(namespace, parent_namespace, metadata_json, created_by, "
                "created_at, updated_at) VALUES (?, NULL, '{}', 'test', ?, ?)",
                [
                    (namespace, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
                    for namespace in present_namespaces
                ],
            )
            cursor.executemany(
                "INSERT INTO object_links "
                "(id, src_oid, relation, dst_oid, metadata_json, created_by, created_at) "
                "VALUES (?, ?, 'references', ?, '{}', 'test', ?)",
                [
                    ("link_20", values[10], values[810], "2026-01-01T00:00:00Z"),
                    ("link_03", values[401], values[1_201], "2026-01-01T00:00:00Z"),
                ],
            )

        selected_namespaces = repository._rows_by_values(
            "object_namespaces",
            "namespace",
            [*reversed(values), values[2]],
        )
        assert [row["namespace"] for row in selected_namespaces] == sorted(
            present_namespaces
        )
        selected_links = repository._object_link_rows(
            [*reversed(values), values[10]]
        )
        assert [row["id"] for row in selected_links] == ["link_03", "link_20"]

        prepared = repository.prepare_checkpoint_restore_process_rows(
            SnapshotRows(
                processes=tuple(_restore_row(f"pid_{value}") for value in values)
            ),
            restored_capability_rows=(),
        )
        assert len(prepared) == len(values)

        object_oids = set(values)
        pids = [f"pid_{value}" for value in values]
        assert repository.registered_jit_tool_ids_for_processes(pids) == frozenset()
        assert repository._externally_borrowed_oids("pid_owner", object_oids) == set()
        with repository.transaction() as cursor:
            repository._invalidate_checkpoint_capability_reservations(
                cursor,
                pids=pids,
                object_oids=object_oids,
            )
            repository._delete_non_checkpoint_capabilities(cursor, pids)
            repository._delete_checkpoint_object_capabilities(cursor, object_oids)
            repository._delete_checkpoint_resource_reservations(cursor, pids)
            repository._delete_process_exec_scope(
                cursor,
                pid="pid_owner",
                object_oids=values,
                namespace_names=values,
            )

        assert parameter_counts
        assert max(parameter_counts) <= 999
        assert len(parameter_counts) > 20
        assert repository._rows_by_values(
            "object_namespaces",
            "namespace",
            values,
        ) == []
        assert repository._object_link_rows(values) == []


@pytest.mark.parametrize("backend", STORE_BACKENDS)
def test_bulk_restore_epoch_reservation_is_exact_monotonic_and_deterministic(
    backend: str,
) -> None:
    with _store_for_backend(backend) as store:
        first = store.reserve_process_restore_epochs(
            (
                ProcessRestoreEpoch(
                    pid="pid_z",
                    revision=8,
                    execution_generation=3,
                    state_generation=11,
                ),
                ProcessRestoreEpoch(
                    pid="pid_a",
                    revision=2,
                    execution_generation=5,
                    state_generation=1,
                ),
            )
        )
        assert [item.pid for item in first] == ["pid_a", "pid_z"]
        assert first == (
            ProcessRestoreEpoch(
                pid="pid_a",
                revision=3,
                execution_generation=6,
                state_generation=2,
            ),
            ProcessRestoreEpoch(
                pid="pid_z",
                revision=9,
                execution_generation=4,
                state_generation=12,
            ),
        )

        second = store.reserve_process_restore_epochs(
            (
                ProcessRestoreEpoch(
                    pid="pid_z",
                    revision=1,
                    execution_generation=1,
                    state_generation=1,
                ),
                ProcessRestoreEpoch(
                    pid="pid_a",
                    revision=20,
                    execution_generation=4,
                    state_generation=30,
                ),
            )
        )
        assert second == (
            ProcessRestoreEpoch(
                pid="pid_a",
                revision=21,
                execution_generation=7,
                state_generation=31,
            ),
            ProcessRestoreEpoch(
                pid="pid_z",
                revision=10,
                execution_generation=5,
                state_generation=13,
            ),
        )


@pytest.mark.parametrize("backend", STORE_BACKENDS)
def test_bulk_restore_epoch_reservation_rejects_duplicate_pids_without_writes(
    backend: str,
) -> None:
    with _store_for_backend(backend) as store:
        duplicate = ProcessRestoreEpoch(
            pid="pid_duplicate",
            revision=4,
            execution_generation=5,
            state_generation=6,
        )
        with pytest.raises(ValidationError, match="duplicate PIDs"):
            store.reserve_process_restore_epochs((duplicate, duplicate))

        assert store.reserve_process_restore_epochs(
            (
                ProcessRestoreEpoch(
                    pid="pid_duplicate",
                    revision=0,
                    execution_generation=0,
                    state_generation=0,
                ),
            )
        ) == (
            ProcessRestoreEpoch(
                pid="pid_duplicate",
                revision=1,
                execution_generation=1,
                state_generation=1,
            ),
        )


@contextlib.contextmanager
def _store_for_backend(
    backend: str,
) -> Iterator[SQLiteStore | PostgresStore]:
    if backend == "sqlite":
        store: SQLiteStore | PostgresStore = SQLiteStore(":memory:")
        try:
            yield store
        finally:
            store.close()
        return
    if backend != "postgres":
        raise AssertionError(f"unknown backend: {backend}")
    with _postgres_schema_dsn() as dsn:
        store = PostgresStore(dsn)
        try:
            yield store
        finally:
            store.close()


@contextlib.contextmanager
def _postgres_schema_dsn() -> Iterator[str]:
    dsn = os.environ["AGENT_LIBOS_POSTGRES_DSN"]
    schema = f"agent_libos_snapshot_batch_{uuid4().hex}"
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    try:
        parsed = urlsplit(dsn)
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key != "options"
        ]
        query.append(("options", f"-csearch_path={schema}"))
        yield urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(query),
                parsed.fragment,
            )
        )
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )

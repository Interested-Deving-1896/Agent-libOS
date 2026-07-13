from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent_libos.storage.sqlite as sqlite_backend
from agent_libos.models import (
    AgentProcess,
    Capability,
    CapabilityStatus,
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    ProcessStatus,
    ResourceBudget,
    ResourceUsage,
)
from agent_libos.models.exceptions import ValidationError
from agent_libos.runtime.external_effects import (
    abandon_external_effect_intent,
    record_external_effect,
)
from agent_libos.storage import SQLiteStore
from agent_libos.storage.postgres import PostgresStore
from agent_libos.utils.ids import utc_now
from tests.support.external_effects import begin_external_effect_intent


class _ConnectionProxy:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @property
    def row_factory(self) -> Any:
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._connection.row_factory = value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class TestExternalEffectIntentRecovery:
    def test_intent_finalization_is_identity_bound_idempotent_and_reserves_state_metadata(self) -> None:
        store = SQLiteStore(':memory:')
        classification = ExternalEffectClassification(
            rollback_class=ExternalEffectRollbackClass.IRREVERSIBLE,
            rollback_status=ExternalEffectRollbackStatus.NOT_SUPPORTED,
            state_mutation=True,
            information_flow=False,
            metadata={'effect_state': 'pending', 'provider_value': 'kept'},
        )
        try:
            intent = begin_external_effect_intent(
                store,
                pid='pid_effect',
                provider='test-provider',
                operation='write',
                target='test:target',
                state_mutation=True,
                information_flow=False,
                metadata={'effect_state': 'forged', 'outcome': 'forged'},
            )
            pending = store.list_external_effects(pid='pid_effect')[0]
            assert pending.effect_state == 'pending'
            assert pending.provider_metadata['effect_state'] == 'pending'
            assert pending.provider_metadata['outcome'] == 'unknown_after_provider_boundary'

            finalized = record_external_effect(
                store,
                pid='pid_effect',
                provider='test-provider',
                operation='write',
                target='test:target',
                classification=classification,
                audit_record=None,
                event=None,
                metadata={'effect_state': 'forged'},
                intent_effect_id=intent.effect_id,
            )
            rows = store.list_external_effects(pid='pid_effect')
            assert len(rows) == 1
            assert finalized.effect_id == intent.effect_id == rows[0].effect_id
            assert rows[0].effect_state == 'finalized'
            assert rows[0].provider_metadata['effect_state'] == 'finalized'
            assert rows[0].provider_metadata['provider_value'] == 'kept'

            with pytest.raises(ValidationError, match='record id must match'):
                store.finalize_external_effect(intent.effect_id, replace(finalized, effect_id='wrong_effect'))
            with pytest.raises(ValidationError, match='must be finalized'):
                store.finalize_external_effect(intent.effect_id, replace(finalized, effect_state='pending'))

            with pytest.raises(ValidationError, match='already finalized'):
                record_external_effect(
                    store,
                    pid='pid_effect',
                    provider='test-provider',
                    operation='write',
                    target='test:target',
                    classification=classification,
                    audit_record=None,
                    event=None,
                    intent_effect_id=intent.effect_id,
                )

            mismatched = begin_external_effect_intent(
                store,
                pid='pid_effect',
                provider='test-provider',
                operation='read',
                target='test:other',
                state_mutation=False,
                information_flow=True,
            )
            with pytest.raises(ValidationError, match='did not match'):
                record_external_effect(
                    store,
                    pid='pid_effect',
                    provider='wrong-provider',
                    operation='read',
                    target='test:other',
                    classification=classification,
                    audit_record=None,
                    event=None,
                    intent_effect_id=mismatched.effect_id,
                )
            remaining = [row for row in store.list_external_effects(pid='pid_effect') if row.effect_id == mismatched.effect_id]
            assert len(remaining) == 1 and remaining[0].effect_state == 'pending'
        finally:
            store.close()

    def test_intent_abandon_is_pending_only_and_not_repeatable(self) -> None:
        store = SQLiteStore(':memory:')
        try:
            intent = begin_external_effect_intent(
                store,
                pid='pid_effect',
                provider='test-provider',
                operation='read',
                target='test:target',
                state_mutation=False,
                information_flow=True,
            )
            abandon_external_effect_intent(store, intent.effect_id)
            assert store.list_external_effects(pid='pid_effect') == []
            with pytest.raises(ValidationError, match='missing or already finalized'):
                abandon_external_effect_intent(store, intent.effect_id)
        finally:
            store.close()


class _FinalizeFailureConnection(_ConnectionProxy):
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        commit_failures: int = 0,
        rollback_failures: int = 0,
        release_failures: int = 0,
    ) -> None:
        super().__init__(connection)
        self.commit_failures = commit_failures
        self.rollback_failures = rollback_failures
        self.release_failures = release_failures

    def commit(self) -> None:
        if self.commit_failures:
            self.commit_failures -= 1
            raise RuntimeError("injected commit failure")
        self._connection.commit()

    def rollback(self) -> None:
        if self.rollback_failures:
            self.rollback_failures -= 1
            raise RuntimeError("injected rollback failure")
        self._connection.rollback()

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        if sql.lstrip().upper().startswith("RELEASE SAVEPOINT") and self.release_failures:
            self.release_failures -= 1
            raise RuntimeError("injected release failure")
        return self._connection.execute(sql, parameters)


class _ExecuteFailureConnection(_ConnectionProxy):
    def __init__(self, connection: sqlite3.Connection, *, marker: str) -> None:
        super().__init__(connection)
        self.marker = marker
        self.failed = False

    def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
        if self.marker in sql and not self.failed:
            self.failed = True
            raise RuntimeError(f"injected SQL failure at {self.marker}")
        return self._connection.execute(sql, parameters)

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        cursor = self._connection.cursor(*args, **kwargs)
        owner = self

        class _FailingCursor:
            def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
                if owner.marker in sql and not owner.failed:
                    owner.failed = True
                    raise RuntimeError(f"injected SQL failure at {owner.marker}")
                return cursor.execute(sql, parameters)

            def __getattr__(self, name: str) -> Any:
                return getattr(cursor, name)

        return _FailingCursor()


def _runnable_process(pid: str) -> AgentProcess:
    now = utc_now()
    return AgentProcess(
        pid=pid,
        parent_pid=None,
        image_id="base-agent:v0",
        status=ProcessStatus.RUNNABLE,
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


def _finite_capability(cap_id: str) -> Capability:
    return Capability(
        cap_id=cap_id,
        subject="pid_test",
        resource="clock:now",
        rights={"read"},
        constraints={},
        issued_by="test",
        issued_at=utc_now(),
        uses_remaining=2,
    )


def _create_legacy_objects_table(connection: sqlite3.Connection, table: str = "objects") -> None:
    connection.execute(
        f"""
        CREATE TABLE {table} (
          oid TEXT PRIMARY KEY,
          name TEXT NOT NULL UNIQUE,
          type TEXT NOT NULL,
          schema_version TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          metadata_json TEXT NOT NULL,
          provenance_json TEXT NOT NULL,
          version INTEGER NOT NULL,
          immutable INTEGER NOT NULL,
          created_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "obj_legacy",
            "legacy.object",
            "artifact",
            "1",
            "{}",
            "{}",
            "{}",
            1,
            1,
            "pid_legacy",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    connection.commit()


class TestStoreTransactionRecovery:
    def test_set_object_payload_sql_failure_restores_previous_payload(self) -> None:
        store = SQLiteStore(":memory:")
        try:
            store.set_object_payload("obj_payload", {"value": "before"})
            store.conn = _ExecuteFailureConnection(
                store.conn,
                marker="UPDATE objects SET payload_json",
            )

            with pytest.raises(RuntimeError, match="injected SQL failure"):
                store.set_object_payload("obj_payload", {"value": "after"})

            assert store.object_payload("obj_payload") == {"value": "before"}
        finally:
            store.close()

    def test_set_object_payload_commit_failure_restores_previous_payload(self) -> None:
        store = SQLiteStore(":memory:")
        try:
            store.set_object_payload("obj_payload", {"value": "before"})
            store.conn = _FinalizeFailureConnection(store.conn, commit_failures=1)

            with pytest.raises(RuntimeError, match="injected commit failure"):
                store.set_object_payload("obj_payload", {"value": "after"})

            assert store.object_payload("obj_payload") == {"value": "before"}
        finally:
            store.close()

    def test_commit_failure_rolls_back_sql_and_object_payload_snapshot(self) -> None:
        store = SQLiteStore(":memory:")
        try:
            store.set_object_payload("obj_payload", {"value": "before"})
            store.conn = _FinalizeFailureConnection(store.conn, commit_failures=1)

            with pytest.raises(RuntimeError, match="injected commit failure"):
                with store.transaction(include_object_payloads=True) as cursor:
                    store.set_object_payload("obj_payload", {"value": "after"})
                    cursor.execute(
                        "INSERT INTO object_namespaces VALUES (?, ?, ?, ?, ?, ?)",
                        ("commit-failed", None, "{}", "test", "1", "1"),
                    )

            assert store.object_payload("obj_payload") == {"value": "before"}
            assert store.select_table_rows(
                "object_namespaces", "namespace = ?", ("commit-failed",)
            ) == []

            with store.transaction() as cursor:
                cursor.execute(
                    "INSERT INTO object_namespaces VALUES (?, ?, ?, ?, ?, ?)",
                    ("healthy-after-rollback", None, "{}", "test", "1", "1"),
                )
            assert store.get_namespace("healthy-after-rollback") is not None
        finally:
            store.close()

    def test_rollback_failure_poison_closes_store(self) -> None:
        store = SQLiteStore(":memory:")
        store.conn = _FinalizeFailureConnection(store.conn, commit_failures=1, rollback_failures=1)
        try:
            with pytest.raises(ValidationError, match="unusable.*rollback failure"):
                with store.transaction() as cursor:
                    cursor.execute(
                        "INSERT INTO object_namespaces VALUES (?, ?, ?, ?, ?, ?)",
                        ("uncertain", None, "{}", "test", "1", "1"),
                    )

            with pytest.raises(ValidationError, match="unusable.*rollback failure"):
                store.list_processes()
        finally:
            store.close()

    def test_release_savepoint_failure_rolls_back_nested_sql_and_payload(self) -> None:
        store = SQLiteStore(":memory:")
        try:
            store.set_object_payload("obj_payload", {"value": "before"})
            store.conn = _FinalizeFailureConnection(store.conn, release_failures=1)

            with store.transaction() as outer:
                outer.execute(
                    "INSERT INTO object_namespaces VALUES (?, ?, ?, ?, ?, ?)",
                    ("outer-before", None, "{}", "test", "1", "1"),
                )
                with pytest.raises(RuntimeError, match="injected release failure"):
                    with store.transaction(include_object_payloads=True) as inner:
                        store.set_object_payload("obj_payload", {"value": "after"})
                        inner.execute(
                            "INSERT INTO object_namespaces VALUES (?, ?, ?, ?, ?, ?)",
                            ("inner-failed", None, "{}", "test", "1", "1"),
                        )
                outer.execute(
                    "INSERT INTO object_namespaces VALUES (?, ?, ?, ?, ?, ?)",
                    ("outer-after", None, "{}", "test", "1", "1"),
                )

            assert store.object_payload("obj_payload") == {"value": "before"}
            namespaces = {
                row["namespace"] for row in store.select_table_rows("object_namespaces")
            }
            assert {"outer-before", "outer-after"}.issubset(namespaces)
            assert "inner-failed" not in namespaces
        finally:
            store.close()

    def test_release_failure_with_failed_savepoint_rollback_poison_closes_store(self) -> None:
        store = SQLiteStore(":memory:")
        store.conn = _FinalizeFailureConnection(store.conn, release_failures=2)
        try:
            with pytest.raises(ValidationError, match="unusable.*rollback failure"):
                with store.transaction():
                    with store.transaction() as inner:
                        inner.execute(
                            "INSERT INTO object_namespaces VALUES (?, ?, ?, ?, ?, ?)",
                            ("nested-uncertain", None, "{}", "test", "1", "1"),
                        )

            with pytest.raises(ValidationError, match="unusable.*rollback failure"):
                store.list_processes()
        finally:
            store.close()

    def test_claim_runnable_process_does_not_commit_outer_transaction(self) -> None:
        store = SQLiteStore(":memory:")
        try:
            store.insert_process(_runnable_process("pid_claim"))
            with pytest.raises(RuntimeError, match="rollback claim"):
                with store.transaction():
                    claimed = store.claim_runnable_process("pid_claim")
                    assert claimed is not None
                    assert claimed.status == ProcessStatus.RUNNING
                    raise RuntimeError("rollback claim")

            process = store.get_process("pid_claim")
            assert process is not None
            assert process.status == ProcessStatus.RUNNABLE
        finally:
            store.close()

    def test_consume_capability_uses_does_not_commit_outer_transaction(self) -> None:
        store = SQLiteStore(":memory:")
        try:
            store.insert_capability(_finite_capability("cap_nested"))
            with pytest.raises(RuntimeError, match="rollback consume"):
                with store.transaction():
                    consumed = store.consume_capability_uses("cap_nested")
                    assert consumed is not None
                    assert consumed.uses_remaining == 1
                    raise RuntimeError("rollback consume")

            capability = store.get_capability("cap_nested")
            assert capability is not None
            assert capability.uses_remaining == 2
            assert capability.status == CapabilityStatus.ACTIVE
        finally:
            store.close()


class TestSQLiteRuntimeLeaseRecovery:
    def test_runtime_lease_refuses_symlink_without_modifying_target(self, tmp_path: Path) -> None:
        if sqlite_backend.fcntl is None or not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("O_NOFOLLOW lease path is not used on this platform")
        db_path = tmp_path / "runtime.sqlite"
        lease_path = db_path.with_suffix(db_path.suffix + ".runtime.lock")
        target = tmp_path / "must-not-change.txt"
        target.write_text("sentinel", encoding="utf-8")
        try:
            lease_path.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation is not available in this environment")

        opened: SQLiteStore | None = None
        try:
            with pytest.raises(ValidationError, match="unsafe runtime lease"):
                opened = SQLiteStore(db_path)
        finally:
            if opened is not None:
                opened.close()

        assert target.read_text(encoding="utf-8") == "sentinel"

    def test_runtime_lease_requires_regular_file_from_fstat(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if sqlite_backend.fcntl is None:
            pytest.skip("file lease is not used on this platform")
        real_fstat = sqlite_backend.os.fstat

        def non_regular_lease(fd: int) -> Any:
            result = real_fstat(fd)
            return SimpleNamespace(st_mode=stat.S_IFDIR | (result.st_mode & 0o777))

        monkeypatch.setattr(sqlite_backend.os, "fstat", non_regular_lease)
        with pytest.raises(ValidationError, match="regular file"):
            SQLiteStore(tmp_path / "runtime.sqlite")

    def test_fcntl_fallback_ignores_stale_legacy_lockfile(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sqlite_backend, "fcntl", None)
        db_path = tmp_path / "runtime.sqlite"
        lease_path = db_path.with_suffix(db_path.suffix + ".runtime.lock")
        lease_path.write_text("2000-01-01T00:00:00+00:00\n999999999\n", encoding="utf-8")

        store = SQLiteStore(db_path)
        try:
            assert store.list_processes() == []
        finally:
            store.close()

    def test_sqlite_connection_uses_same_canonical_path_as_lease(self, tmp_path: Path) -> None:
        target = tmp_path / "canonical.sqlite"
        sqlite3.connect(target).close()
        alias = tmp_path / "alias.sqlite"
        try:
            alias.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation is not available in this environment")

        store = SQLiteStore(alias)
        try:
            database_row = store.conn.execute("PRAGMA database_list").fetchone()
            assert database_row is not None
            assert Path(database_row["file"]) == target.resolve()
            assert store.path == str(alias)
        finally:
            store.close()


class _PostgresResult:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def fetchone(self) -> dict[str, Any]:
        return self.row


class _PostgresLeaseConnection:
    def __init__(self, database: str, schema: str) -> None:
        self.database = database
        self.schema = schema
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: Any = ()) -> _PostgresResult:
        selected = tuple(params)
        self.calls.append((sql, selected))
        if "current_database()" in sql:
            return _PostgresResult({"database_name": self.database, "schema_name": self.schema})
        return _PostgresResult({"acquired": True})


class TestPostgresRuntimeLeaseIsolation:
    def test_advisory_lease_key_isolated_by_database_and_schema(self) -> None:
        keys: list[int] = []
        stores: list[tuple[PostgresStore, _PostgresLeaseConnection]] = []
        for database, schema in (("db_a", "schema_a"), ("db_a", "schema_b"), ("db_b", "schema_a")):
            store = PostgresStore.__new__(PostgresStore)
            store._runtime_lease_acquired = False
            connection = _PostgresLeaseConnection(database, schema)
            store._acquire_runtime_lease(connection)  # type: ignore[arg-type]
            stores.append((store, connection))
            lock_calls = [call for call in connection.calls if "pg_try_advisory_lock" in call[0]]
            assert len(lock_calls) == 1
            keys.append(int(lock_calls[0][1][0]))

        assert len(set(keys)) == 3

        for store, connection in stores:
            store._release_runtime_lease(connection)  # type: ignore[arg-type]
            unlock_calls = [call for call in connection.calls if "pg_advisory_unlock" in call[0]]
            assert unlock_calls == [("SELECT pg_advisory_unlock(?)", (store._runtime_lease_key,))]


class TestObjectSchemaMigrationRecovery:
    def test_objects_rebuild_failure_is_atomic_and_next_open_recovers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = tmp_path / "legacy.sqlite"
        connection = sqlite3.connect(db_path)
        try:
            _create_legacy_objects_table(connection)
        finally:
            connection.close()

        real_connect = sqlite_backend.sqlite3.connect
        injected = False

        def connect_with_failure(*args: Any, **kwargs: Any) -> Any:
            nonlocal injected
            raw = real_connect(*args, **kwargs)
            if not injected:
                injected = True
                return _ExecuteFailureConnection(raw, marker="DROP TABLE objects_old")
            return raw

        monkeypatch.setattr(sqlite_backend.sqlite3, "connect", connect_with_failure)
        with pytest.raises(RuntimeError, match="injected SQL failure"):
            SQLiteStore(db_path)

        reopened = SQLiteStore(db_path)
        try:
            rows = reopened.select_table_rows("objects", "oid = ?", ("obj_legacy",))
            assert len(rows) == 1
            assert rows[0]["namespace"] == "system"
        finally:
            reopened.close()

    def test_open_recovers_preexisting_objects_old_without_losing_rows(self, tmp_path: Path) -> None:
        db_path = tmp_path / "interrupted.sqlite"
        connection = sqlite3.connect(db_path)
        try:
            _create_legacy_objects_table(connection, table="objects_old")
        finally:
            connection.close()

        store = SQLiteStore(db_path)
        try:
            rows = store.select_table_rows("objects", "oid = ?", ("obj_legacy",))
            assert len(rows) == 1
            assert rows[0]["name"] == "legacy.object"
        finally:
            store.close()

        connection = sqlite3.connect(db_path)
        try:
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'objects_old'"
            ).fetchone() is None
        finally:
            connection.close()

from __future__ import annotations

from pathlib import Path
import os
import stat

import pytest

import agent_libos.storage.sqlite as sqlite_backend
from agent_libos.models.exceptions import ValidationError
from agent_libos.storage.factory import _sqlite_target
from agent_libos.storage.postgres import _PostgresCursor, _PostgresDialect
from agent_libos.storage import PostgresStore, SQLRuntimeStore, SQLiteStore


ROOT = Path(__file__).resolve().parents[2]
AGENT_LIBOS = ROOT / "agent_libos"
STORAGE_BACKENDS = {
    AGENT_LIBOS / "storage" / "sqlite.py",
    AGENT_LIBOS / "storage" / "postgres.py",
}


class TestStorageBackendBoundaries:
    def test_sqlite_database_lease_and_wal_sidecars_are_owner_only(self, tmp_path: Path) -> None:
        db_path = tmp_path / "private-runtime.sqlite"
        previous_umask = os.umask(0o022)
        try:
            store = SQLiteStore(db_path)
        finally:
            os.umask(previous_umask)
        try:
            assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
            lease_path = db_path.with_suffix(db_path.suffix + ".runtime.lock")
            if sqlite_backend.fcntl is not None and hasattr(os, "O_NOFOLLOW"):
                assert stat.S_IMODE(lease_path.stat().st_mode) == 0o600

            store.conn.execute("PRAGMA journal_mode=WAL")
            store.conn.execute("CREATE TABLE private_mode_probe(value TEXT)")
            store.conn.execute("INSERT INTO private_mode_probe VALUES ('secret')")
            store.conn.commit()
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{db_path}{suffix}")
                assert sidecar.exists()
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
        finally:
            store.close()

    def test_sqlite_reopen_tightens_owner_database_and_lease_modes(self, tmp_path: Path) -> None:
        if sqlite_backend.fcntl is None or not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("POSIX secure runtime lease is unavailable")
        db_path = tmp_path / "existing-runtime.sqlite"
        store = SQLiteStore(db_path)
        store.close()
        lease_path = db_path.with_suffix(db_path.suffix + ".runtime.lock")
        db_path.chmod(0o644)
        lease_path.chmod(0o644)

        reopened = SQLiteStore(db_path)
        try:
            assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(lease_path.stat().st_mode) == 0o600
        finally:
            reopened.close()

    def test_runtime_code_does_not_reach_into_store_connection(self) -> None:
        offenders: list[str] = []
        for path in AGENT_LIBOS.rglob("*.py"):
            if path in STORAGE_BACKENDS:
                continue
            text = path.read_text(encoding="utf-8")
            if "store.conn" in text:
                offenders.append(path.relative_to(ROOT).as_posix())

        assert offenders == []

    def test_runtime_code_does_not_reach_into_store_private_fields(self) -> None:
        offenders: list[str] = []
        allowed_fragments = (
            "runtime.store._jit_sources",
            "runtime.store._handles",
        )
        for path in AGENT_LIBOS.rglob("*.py"):
            if path in STORAGE_BACKENDS:
                continue
            text = path.read_text(encoding="utf-8")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if "store._" not in line:
                    continue
                if any(fragment in line for fragment in allowed_fragments):
                    continue
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{line_number}")

        assert offenders == []

    def test_sqlite_import_is_confined_to_sqlite_backend(self) -> None:
        offenders: list[str] = []
        for path in AGENT_LIBOS.rglob("*.py"):
            if path == AGENT_LIBOS / "storage" / "sqlite.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "import sqlite3" in text or "from sqlite3" in text:
                offenders.append(path.relative_to(ROOT).as_posix())

        assert offenders == []

    def test_postgres_store_does_not_inherit_sqlite_backend(self) -> None:
        assert issubclass(SQLiteStore, SQLRuntimeStore)
        assert issubclass(PostgresStore, SQLRuntimeStore)
        assert not issubclass(PostgresStore, SQLiteStore)

    def test_postgres_backend_does_not_export_global_sql_rewriter(self) -> None:
        text = (AGENT_LIBOS / "storage" / "postgres.py").read_text(encoding="utf-8")

        assert "def _postgres_sql" not in text
        assert "class PostgresStore(SQLRuntimeStore)" in text
        assert "pg_try_advisory_lock" in text
        assert "pg_advisory_unlock" in text

    def test_sqlite_runtime_lease_uses_atomic_lockfile_without_fcntl(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sqlite_backend, "fcntl", None)
        db_path = tmp_path / "runtime.sqlite"
        first = SQLiteStore(db_path)
        try:
            with pytest.raises(ValidationError, match="already open"):
                SQLiteStore(db_path)
        finally:
            first.close()

        assert not db_path.with_suffix(db_path.suffix + ".runtime.lock").exists()
        second = SQLiteStore(db_path)
        second.close()

    def test_sqlite_runtime_lease_rejects_symlink_alias(self, tmp_path: Path) -> None:
        db_path = tmp_path / "runtime.sqlite"
        alias = tmp_path / "runtime-alias.sqlite"
        first = SQLiteStore(db_path)
        try:
            try:
                alias.symlink_to(db_path)
            except OSError:
                pytest.skip("symlink creation is not available in this environment")
            with pytest.raises(ValidationError, match="already open"):
                SQLiteStore(alias)
        finally:
            first.close()

        reopened = SQLiteStore(alias)
        reopened.close()

    def test_nested_store_transactions_use_savepoints(self) -> None:
        store = SQLiteStore(":memory:")
        try:
            with store.transaction() as outer:
                outer.execute(
                    "INSERT INTO object_namespaces VALUES (?, ?, ?, ?, ?, ?)",
                    ("outer", None, "{}", "test", "1", "1"),
                )
                with pytest.raises(RuntimeError, match="rollback inner"):
                    with store.transaction() as inner:
                        inner.execute(
                            "INSERT INTO object_namespaces VALUES (?, ?, ?, ?, ?, ?)",
                            ("inner", None, "{}", "test", "1", "1"),
                        )
                        raise RuntimeError("rollback inner")
                outer.execute(
                    "INSERT INTO object_namespaces VALUES (?, ?, ?, ?, ?, ?)",
                    ("after", None, "{}", "test", "1", "1"),
                )

            namespaces = {
                row["namespace"] for row in store.select_table_rows("object_namespaces", order_by="namespace")
            }
            assert {"after", "outer"}.issubset(namespaces)
            assert "inner" not in namespaces
        finally:
            store.close()

    def test_sqlite_uri_normalizes_posix_absolute_paths(self) -> None:
        assert _sqlite_target("sqlite:////tmp/agent-libos.sqlite") == "/tmp/agent-libos.sqlite"
        assert _sqlite_target("sqlite:///C:/agent-libos/runtime.sqlite") == "C:/agent-libos/runtime.sqlite"

    def test_postgres_cursor_does_not_pass_empty_params_for_percent_literals(self) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def execute(self, *args: object) -> None:
                self.calls.append(args)

        fake = FakeCursor()
        cursor = _PostgresCursor(fake, _PostgresDialect())

        cursor.execute("UPDATE objects SET owner_kind = 'process' WHERE created_by LIKE 'process:%'")

        assert fake.calls == [("UPDATE objects SET owner_kind = 'process' WHERE created_by LIKE 'process:%'",)]

    def test_postgres_cursor_escapes_percent_literals_with_params(self) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def execute(self, *args: object) -> None:
                self.calls.append(args)

        fake = FakeCursor()
        cursor = _PostgresCursor(fake, _PostgresDialect())

        cursor.execute("DELETE FROM capabilities WHERE subject IN (?) AND resource NOT LIKE 'checkpoint:%'", ("pid_1",))

        assert fake.calls == [
            ("DELETE FROM capabilities WHERE subject IN (%s) AND resource NOT LIKE 'checkpoint:%%'", ("pid_1",))
        ]

    def test_postgres_cursor_preserves_question_mark_literals_with_params(self) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def execute(self, *args: object) -> None:
                self.calls.append(args)

        fake = FakeCursor()
        cursor = _PostgresCursor(fake, _PostgresDialect())

        cursor.execute("SELECT '?' AS literal, ? AS value, 'it''s ?' AS escaped", ("ok",))

        assert fake.calls == [("SELECT '?' AS literal, %s AS value, 'it''s ?' AS escaped", ("ok",))]

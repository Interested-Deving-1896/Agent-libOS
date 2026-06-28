from __future__ import annotations

from pathlib import Path

from agent_libos.storage.postgres import _PostgresCursor, _PostgresDialect
from agent_libos.storage import PostgresStore, SQLRuntimeStore, SQLiteStore


ROOT = Path(__file__).resolve().parents[2]
AGENT_LIBOS = ROOT / "agent_libos"
STORAGE_BACKENDS = {
    AGENT_LIBOS / "storage" / "sqlite.py",
    AGENT_LIBOS / "storage" / "postgres.py",
}


class TestStorageBackendBoundaries:
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

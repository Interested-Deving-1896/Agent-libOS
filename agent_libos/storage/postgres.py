from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any

from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models.exceptions import ValidationError
from agent_libos.storage.sqlite import SQLRuntimeStore


_POSTGRES_RUNTIME_LOCK_KEY = 0x4147454E544C4942


class _PostgresDialect:
    def prepare(self, sql: str, *, with_params: bool = False) -> str:
        text = sql.strip()
        table_match = _PRAGMA_TABLE_INFO.match(text)
        if table_match:
            table = table_match.group("table")
            return (
                "SELECT column_name AS name "
                "FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = "
                f"'{table}'"
            )
        if _PRAGMA_INDEX_LIST.match(text) or _PRAGMA_INDEX_INFO.match(text):
            return "SELECT NULL::text AS name WHERE false"

        was_insert_or_ignore_namespace = bool(
            re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\s+object_namespaces\b", sql, re.IGNORECASE)
        )
        was_insert_or_replace_skill_trust = bool(
            re.search(r"\bINSERT\s+OR\s+REPLACE\s+INTO\s+skill_trust\b", sql, re.IGNORECASE)
        )
        transformed = sql
        transformed = transformed.replace("INSERT OR IGNORE INTO", "INSERT INTO")
        transformed = transformed.replace(
            "INSERT OR REPLACE INTO skill_trust",
            "INSERT INTO skill_trust",
        )
        transformed = transformed.replace("rowid AS _audit_rowid", "record_id AS _audit_rowid")
        transformed = re.sub(r"\browid\b", "record_id", transformed)
        transformed = _prepare_parameterized_sql(transformed) if with_params else transformed.replace("?", "%s")
        if was_insert_or_ignore_namespace and "ON CONFLICT" not in transformed:
            transformed = f"{transformed.rstrip()} ON CONFLICT (namespace) DO NOTHING"
        if was_insert_or_replace_skill_trust and "ON CONFLICT" not in transformed:
            transformed = (
                f"{transformed.rstrip()} "
                "ON CONFLICT (source_type, source, package_sha256) DO UPDATE SET "
                "trust_id = EXCLUDED.trust_id, "
                "trusted_by = EXCLUDED.trusted_by, "
                "created_at = EXCLUDED.created_at, "
                "metadata_json = EXCLUDED.metadata_json"
            )
        return transformed


class _PostgresCursor:
    def __init__(self, cursor: Any, dialect: _PostgresDialect):
        self._cursor = cursor
        self._dialect = dialect

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> "_PostgresCursor":
        selected_params = tuple(params)
        if selected_params:
            prepared = self._dialect.prepare(sql, with_params=True)
            self._cursor.execute(prepared, selected_params)
        else:
            prepared = self._dialect.prepare(sql)
            self._cursor.execute(prepared)
        return self

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> None:
        self._cursor.executemany(self._dialect.prepare(sql, with_params=True), [tuple(params) for params in seq_of_params])

    def fetchone(self) -> dict[str, Any] | None:
        return self._cursor.fetchone()

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._cursor)


class _PostgresConnection:
    def __init__(self, dsn: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised without optional dependency
            raise ValidationError(
                "PostgreSQL runtime store requires the optional dependency; "
                "install with `uv sync --extra postgres --all-groups`"
            ) from exc
        self._conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        self._dialect = _PostgresDialect()

    def close(self) -> None:
        self._conn.close()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def cursor(self) -> _PostgresCursor:
        return _PostgresCursor(self._conn.cursor(), self._dialect)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _PostgresCursor:
        cursor = self.cursor()
        cursor.execute(sql, params)
        return cursor

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> None:
        self.cursor().executemany(sql, seq_of_params)

    def executescript(self, script: str) -> None:
        for statement in _split_sql_script(script):
            if statement:
                self.execute(statement)


class PostgresStore(SQLRuntimeStore):
    """PostgreSQL runtime store backend."""

    def __init__(self, dsn: str, *, config: AgentLibOSConfig | None = None):
        self.config = config or DEFAULT_CONFIG
        self.path = dsn
        self.dsn = dsn
        self._runtime_lease_acquired = False
        conn = _PostgresConnection(dsn)
        try:
            self._acquire_runtime_lease(conn)
            self._init_store(dsn, config=config, conn=conn)
        except Exception:
            self._release_runtime_lease(conn)
            conn.close()
            raise

    def close(self) -> None:
        try:
            self._release_runtime_lease(getattr(self, "conn", None))
        finally:
            super().close()

    def _acquire_runtime_lease(self, conn: _PostgresConnection) -> None:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(?) AS acquired",
            (_POSTGRES_RUNTIME_LOCK_KEY,),
        ).fetchone()
        if not row or not row.get("acquired"):
            raise ValidationError("runtime store is already open: postgres")
        self._runtime_lease_acquired = True

    def _release_runtime_lease(self, conn: _PostgresConnection | None) -> None:
        if conn is None or not getattr(self, "_runtime_lease_acquired", False):
            return
        self._runtime_lease_acquired = False
        try:
            conn.execute("SELECT pg_advisory_unlock(?)", (_POSTGRES_RUNTIME_LOCK_KEY,))
        except Exception:
            pass


_PRAGMA_TABLE_INFO = re.compile(r"^\s*PRAGMA\s+table_info\((?P<table>[A-Za-z_][A-Za-z0-9_]*)\)\s*$", re.IGNORECASE)
_PRAGMA_INDEX_LIST = re.compile(r"^\s*PRAGMA\s+index_list\((?P<table>[A-Za-z_][A-Za-z0-9_]*)\)\s*$", re.IGNORECASE)
_PRAGMA_INDEX_INFO = re.compile(r"^\s*PRAGMA\s+index_info\((?P<index>[A-Za-z_][A-Za-z0-9_]*)\)\s*$", re.IGNORECASE)


def _prepare_parameterized_sql(sql: str) -> str:
    """Convert SQLite placeholders and escape literal percents for psycopg."""

    parts: list[str] = []
    in_quote = False
    quote_char = ""
    index = 0
    while index < len(sql):
        char = sql[index]
        if char in {"'", '"'}:
            parts.append(char)
            if in_quote and char == quote_char:
                if index + 1 < len(sql) and sql[index + 1] == quote_char:
                    parts.append(sql[index + 1])
                    index += 2
                    continue
                in_quote = False
                quote_char = ""
            elif not in_quote:
                in_quote = True
                quote_char = char
        elif char == "?" and not in_quote:
            parts.append("%s")
        elif char == "%":
            parts.append("%%")
        else:
            parts.append(char)
        index += 1
    return "".join(parts)


def _split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_quote = False
    quote_char = ""
    for char in script:
        if char in {"'", '"'}:
            if in_quote and char == quote_char:
                in_quote = False
            elif not in_quote:
                in_quote = True
                quote_char = char
        if char == ";" and not in_quote:
            statements.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements

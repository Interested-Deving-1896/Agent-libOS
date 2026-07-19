from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from agent_libos.config import AgentLibOSConfig, RuntimeDefaults
from agent_libos.models import ProcessStatus
from agent_libos.runtime.runtime import Runtime


STORE_BACKENDS = [
    "sqlite",
    pytest.param("postgres", marks=pytest.mark.postgres),
]


@pytest.mark.parametrize("backend", STORE_BACKENDS)
def test_stale_execution_recovery_processes_every_page_with_bounded_diagnostics(
    backend: str,
    tmp_path: Path,
) -> None:
    with _runtime_target(backend, tmp_path) as (target, config):
        runtime = Runtime.open(target, config=config)
        pids = [runtime.process.spawn(goal=f"stale execution {index}") for index in range(5)]
        for pid in pids:
            assert runtime.store.claim_execution(pid, owner_id="crashed-runtime") is not None
        runtime.close()

        reopened = Runtime.open(target, config=config)
        try:
            summary = reopened.recovered_stale_executions
            assert summary.total_count == len(pids)
            assert len(summary.sample_pids) == 2
            assert summary.truncated
            assert {
                process.pid
                for process in reopened.store.list_processes()
                if process.status == ProcessStatus.PAUSED
            } == set(pids)
            audit_rows = reopened.store.select_table_rows(
                "audit_records",
                "action = ?",
                ("stale_execution_recovery",),
            )
            event_rows = reopened.store.select_table_rows(
                "events",
                "source = ? AND type = ?",
                ("runtime.recovery", "process_signal"),
            )
            assert len(audit_rows) == len(pids)
            assert len(event_rows) == len(pids)
        finally:
            reopened.close()


@pytest.mark.parametrize("backend", STORE_BACKENDS)
def test_stale_execution_recovery_has_status_first_keyset_index(
    backend: str,
    tmp_path: Path,
) -> None:
    with _runtime_target(backend, tmp_path) as (target, config):
        runtime = Runtime.open(target, config=config)
        try:
            if backend == "sqlite":
                plan = runtime.store._query(
                    "EXPLAIN QUERY PLAN SELECT pid FROM processes "
                    "WHERE status = ? "
                    "AND (execution_owner_id IS NULL OR execution_owner_id <> ?) "
                    "AND pid > ? ORDER BY pid LIMIT ?",
                    ("running", "current-runtime", "pid-cursor", 2),
                )
                assert any(
                    "IDX_PROCESSES_EXECUTION_RECOVERY" in str(row["detail"]).upper()
                    for row in plan
                )
            else:
                indexes = runtime.store._query(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = current_schema() AND tablename = ?",
                    ("processes",),
                )
                assert any(
                    "(status, pid, execution_owner_id)" in str(row["indexdef"])
                    for row in indexes
                )
        finally:
            runtime.close()


@contextlib.contextmanager
def _runtime_target(
    backend: str,
    tmp_path: Path,
) -> Iterator[tuple[str | Path, AgentLibOSConfig]]:
    if backend == "sqlite":
        yield (
            tmp_path / "stale-execution-recovery.sqlite",
            AgentLibOSConfig(
                runtime=RuntimeDefaults(
                    operation_recovery_page_size=2,
                    operation_recovery_page_hard_limit=3,
                )
            ),
        )
        return
    if backend != "postgres":
        raise AssertionError(f"unknown backend: {backend}")
    with _postgres_schema_dsn() as dsn:
        yield (
            dsn,
            AgentLibOSConfig(
                runtime=RuntimeDefaults(
                    store_backend="postgres",
                    store_dsn=dsn,
                    operation_recovery_page_size=2,
                    operation_recovery_page_hard_limit=3,
                )
            ),
        )


@contextlib.contextmanager
def _postgres_schema_dsn() -> Iterator[str]:
    dsn = os.environ["AGENT_LIBOS_POSTGRES_DSN"]
    schema = f"agent_libos_stale_execution_{uuid4().hex}"
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
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
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )

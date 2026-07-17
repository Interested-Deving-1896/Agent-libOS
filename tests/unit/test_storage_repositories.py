from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any

import pytest

from agent_libos.models import AgentProcess, ObjectNamespace, ProcessStatus, ResourceBudget, ResourceUsage
from agent_libos.storage import (
    AuthorityRepository,
    EvidenceRepository,
    ExtensionRepository,
    ObjectRepository,
    ProcessRepository,
    SqlEngine,
    SqlSession,
    SQLiteStore,
    UnitOfWork,
)
from agent_libos.storage.postgres import _PostgresConnection, _PostgresCursor, _PostgresDialect
from agent_libos.utils.ids import utc_now


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    @contextmanager
    def locked(self):
        yield

    @contextmanager
    def transaction(self, *, include_object_payloads: bool = False):
        self.calls.append(("transaction", (), {"include_object_payloads": include_object_payloads}))
        yield object()

    def __getattr__(self, name: str):
        def record(*args: Any, **kwargs: Any) -> str:
            self.calls.append((name, args, kwargs))
            return name

        return record


def _process(pid: str) -> AgentProcess:
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


def test_unit_of_work_composes_narrow_delegating_repositories() -> None:
    store = _RecordingStore()
    unit = UnitOfWork(store)

    assert isinstance(unit.processes, ProcessRepository)
    assert isinstance(unit.objects, ObjectRepository)
    assert isinstance(unit.authority, AuthorityRepository)
    assert isinstance(unit.evidence, EvidenceRepository)
    assert isinstance(unit.extensions, ExtensionRepository)

    assert unit.processes.get_process("pid") == "get_process"
    assert unit.objects.get_object("oid") == "get_object"
    assert unit.authority.get_capability("cap") == "get_capability"
    assert unit.evidence.get_audit("audit") == "get_audit"
    assert unit.extensions.get_skill("skill") == "get_skill"
    with unit.transaction(include_object_payloads=True) as active:
        assert active is unit

    assert [call[0] for call in store.calls] == [
        "get_process",
        "get_object",
        "get_capability",
        "get_audit",
        "get_skill",
        "transaction",
    ]
    assert store.calls[-1][2] == {"include_object_payloads": True}
    with pytest.raises(AttributeError, match="no repository operation"):
        unit.processes.get_object("oid")


def test_repositories_share_one_outer_transaction_atomically() -> None:
    store = SQLiteStore(":memory:")
    unit = UnitOfWork(store)
    namespace = ObjectNamespace(
        namespace="rolled-back",
        parent_namespace=None,
        metadata={},
        created_by="test",
        created_at="1",
        updated_at="1",
    )
    try:
        with pytest.raises(RuntimeError, match="abort unit"):
            with unit.transaction():
                unit.objects.insert_namespace(namespace)
                unit.processes.insert_process(_process("pid_rolled_back"))
                raise RuntimeError("abort unit")

        assert store.get_namespace("rolled-back") is None
        assert store.get_process("pid_rolled_back") is None

        with unit.transaction():
            unit.objects.insert_namespace(namespace)
            unit.processes.insert_process(_process("pid_committed"))

        assert store.get_namespace("rolled-back") == namespace
        assert store.get_process("pid_committed") is not None
    finally:
        store.close()


class _FakePostgresCursor:
    rowcount = 0

    def execute(self, *_args: Any) -> None:
        return None

    def executemany(self, *_args: Any) -> None:
        return None

    def fetchone(self) -> None:
        return None

    def __iter__(self):
        return iter(())


def test_sqlite_and_postgres_adapters_share_sql_contract_shape() -> None:
    sqlite_connection = sqlite3.connect(":memory:")
    sqlite_cursor = sqlite_connection.cursor()
    postgres_connection = _PostgresConnection.__new__(_PostgresConnection)
    postgres_cursor = _PostgresCursor(_FakePostgresCursor(), _PostgresDialect())
    try:
        assert isinstance(sqlite_connection, SqlEngine)
        assert isinstance(sqlite_cursor, SqlSession)
        assert isinstance(postgres_connection, SqlEngine)
        assert isinstance(postgres_cursor, SqlSession)
    finally:
        sqlite_cursor.close()
        sqlite_connection.close()

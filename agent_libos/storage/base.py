from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol

from agent_libos.storage.engine import SqlSession


class StoreTransaction(SqlSession, Protocol):
    """Backend-neutral cursor used inside a UnitOfWork transaction."""


class RuntimeStore(Protocol):
    """Narrow host boundary for a concrete runtime store.

    Domain persistence is intentionally absent. Callers that need process,
    object, authority, evidence, or extension records consume the matching
    repository from :class:`UnitOfWork`.
    """

    config: Any
    path: str

    def close(self) -> None:
        ...

    def locked(self) -> AbstractContextManager[None]:
        ...

    def transaction(
        self,
        *,
        include_object_payloads: bool = False,
    ) -> AbstractContextManager[StoreTransaction]:
        ...

    def validate_table_identifier(self, table: str) -> str:
        ...

    def validate_column_identifier(self, table: str, column: str) -> str:
        ...

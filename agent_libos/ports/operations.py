from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol


class OperationPort(Protocol):
    """Narrow evidence surface needed by capability services."""

    def current_id(self) -> str | None:
        ...

    def current(self) -> Any | None:
        ...

    def operation_for_evidence(
        self,
        evidence_types: tuple[str, ...],
        evidence_id: str,
    ) -> list[Any]:
        ...

    def attach(self, operation_id: str) -> AbstractContextManager[Any]:
        ...

    def expect(self, *roles: str, operation_id: str | None = None) -> Any | None:
        ...

    def link_evidence(
        self,
        evidence_type: str,
        evidence_id: str,
        role: str,
        *,
        operation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        ...

    def scope(
        self,
        *,
        kind: str,
        name: str,
        actor: str,
        pid: str | None,
        expected_roles: list[str] | tuple[str, ...] = (),
        operation_id: str | None = None,
        parent_operation_id: str | None = None,
        auto_finish: bool = True,
    ) -> AbstractContextManager[Any]:
        ...

    def finish(self, outcome: str, *, operation_id: str | None = None) -> Any:
        ...

from __future__ import annotations

from typing import Any, Protocol

from agent_libos.models import ResourceUsage


class ResourcePort(Protocol):
    """Resource accounting surface used outside the runtime package."""

    def preflight(
        self,
        pid: str,
        request: ResourceUsage | dict[str, Any],
        *,
        source: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        ...

    def charge(
        self,
        pid: str,
        usage: ResourceUsage | dict[str, Any],
        *,
        source: str,
        context: dict[str, Any] | None = None,
        allow_overage: bool = False,
        kill_on_exceed: bool = True,
    ) -> None:
        ...

    def kill_if_exceeded(
        self,
        pid: str,
        *,
        reason: str,
        owner_pid: str | None = None,
        limit: dict[str, Any] | None = None,
    ) -> None:
        ...

    def has_limit(self, pid: str, budget_field: str) -> bool:
        ...

    def context_materialization_window_limit(self, pid: str) -> int:
        ...

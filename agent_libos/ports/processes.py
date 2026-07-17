from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from agent_libos.models import (
    AgentProcess,
    DataFlowContext,
    DataLabels,
    ObjectHandle,
    ObjectMetadata,
)


class ProcessControlPort(Protocol):
    """Process lifecycle surface needed by cross-subsystem orchestrators."""

    def get(self, pid: str) -> AgentProcess:
        ...

    def exit(
        self,
        pid: str,
        result: ObjectHandle | None = None,
        failed: bool = False,
        message: str | None = None,
        *,
        payload: dict[str, Any] | None = None,
        source_oids: Iterable[str] | None = None,
        source_labels: ObjectMetadata | DataLabels | dict[str, Any] | None = None,
        source_context: DataFlowContext | None = None,
    ) -> ObjectHandle | None:
        ...

    def pause_for_host_resume(self, pid: str, reason: str) -> None:
        ...

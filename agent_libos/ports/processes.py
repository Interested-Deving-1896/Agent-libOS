from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from agent_libos.models import (
    AgentProcess,
    DataFlowContext,
    DataLabels,
    ObjectHandle,
    ObjectMetadata,
    ProcessOutcome,
    ProcessRestoreEpoch,
    ProcessStatus,
    ProcessWaitState,
)


class ProcessRestoreEpochRepositoryPort(Protocol):
    """Bulk durable allocator for checkpoint-restore concurrency epochs."""

    def reserve_process_restore_epochs(
        self,
        floors: Iterable[ProcessRestoreEpoch],
    ) -> tuple[ProcessRestoreEpoch, ...]: ...


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


class ProcessTransitionRepositoryPort(Protocol):
    """Single typed persistence seam for semantic process transitions."""

    def get_process(self, pid: str) -> AgentProcess | None:
        ...

    def apply_process_state_transition(
        self,
        pid: str,
        status: ProcessStatus | str,
        *,
        expected_revision: int,
        expected_status: ProcessStatus | str | None = None,
        expected_state_generation: int | None = None,
        wait_state: ProcessWaitState | None = None,
        outcome: ProcessOutcome | None = None,
        status_message: str | None = None,
        control: bool = False,
        allowed_statuses: Iterable[ProcessStatus | str] | None = None,
        reason: str | None = None,
    ) -> AgentProcess: ...

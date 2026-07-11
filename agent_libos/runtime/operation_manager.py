from __future__ import annotations

import asyncio
import inspect
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from functools import wraps
from typing import Any, Callable, Iterable, Iterator, TypeVar

from agent_libos.models import (
    OperationEvidenceLink,
    OperationEvidenceRole,
    OperationKind,
    OperationOutcome,
    OperationRecord,
    OperationState,
)
from agent_libos.models.exceptions import (
    CapabilityDenied,
    HumanApprovalRequired,
    PolicyDenied,
    ProcessMessageWaitRequired,
    ProcessWaitRequired,
    ResourceLimitExceeded,
)
from agent_libos.storage import RuntimeStore
from agent_libos.utils.ids import new_id, utc_now


@dataclass(frozen=True)
class _CurrentOperation:
    manager_identity: int
    operation_id: str


_CURRENT_OPERATION: ContextVar[_CurrentOperation | None] = ContextVar(
    "agent_libos_current_operation",
    default=None,
)

F = TypeVar("F", bound=Callable[..., Any])


class OperationManager:
    """Durable causal scopes for protected Agent libOS operations."""

    def __init__(self, store: RuntimeStore):
        self.store = store
        self._identity = id(self)

    def current_id(self) -> str | None:
        current = _CURRENT_OPERATION.get()
        if current is None or current.manager_identity != self._identity:
            return None
        return current.operation_id

    def current(self) -> OperationRecord | None:
        operation_id = self.current_id()
        return self.store.get_operation(operation_id) if operation_id is not None else None

    def start(
        self,
        *,
        kind: OperationKind | str,
        name: str,
        actor: str,
        pid: str | None,
        parent_operation_id: str | None = None,
        expected_roles: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
    ) -> OperationRecord:
        parent_id = parent_operation_id if parent_operation_id is not None else self.current_id()
        parent = self.store.get_operation(parent_id) if parent_id is not None else None
        if parent_id is not None and parent is None:
            raise ValueError(f"parent operation not found: {parent_id}")
        operation_id = new_id("op")
        now = utc_now()
        record = OperationRecord(
            operation_id=operation_id,
            root_operation_id=parent.root_operation_id if parent is not None else operation_id,
            parent_operation_id=parent_id,
            kind=OperationKind(kind),
            name=str(name),
            actor=str(actor),
            pid=str(pid) if pid is not None else None,
            state=OperationState.RUNNING,
            outcome=OperationOutcome.PENDING,
            expected_roles=sorted({str(value) for value in expected_roles}),
            metadata=dict(metadata or {}),
            started_at=now,
            updated_at=now,
        )
        self.store.insert_operation(record)
        return record

    def resume(self, operation_id: str) -> OperationRecord:
        with self.store.locked():
            record = self._require(operation_id)
            if record.state == OperationState.TERMINAL:
                return record
            if record.state == OperationState.RUNNING:
                return record
            updated = replace(
                record,
                state=OperationState.RUNNING,
                outcome=OperationOutcome.PENDING,
                updated_at=utc_now(),
                completed_at=None,
            )
            if not self.store.update_operation(updated, expected_states=[OperationState.WAITING.value]):
                return self._require(operation_id)
            return updated

    def expect(self, *roles: OperationEvidenceRole | str, operation_id: str | None = None) -> OperationRecord | None:
        selected_id = operation_id or self.current_id()
        if selected_id is None:
            return None
        with self.store.locked():
            record = self._require(selected_id)
            expected = sorted({*record.expected_roles, *(str(role) for role in roles)})
            if expected == record.expected_roles:
                return record
            updated = replace(record, expected_roles=expected, updated_at=utc_now())
            self.store.update_operation(updated)
            return updated

    def merge_metadata(self, metadata: dict[str, Any], *, operation_id: str | None = None) -> OperationRecord | None:
        selected_id = operation_id or self.current_id()
        if selected_id is None:
            return None
        with self.store.locked():
            record = self._require(selected_id)
            updated = replace(record, metadata={**record.metadata, **dict(metadata)}, updated_at=utc_now())
            self.store.update_operation(updated)
            return updated

    def set_pid(self, pid: str, *, operation_id: str | None = None) -> OperationRecord | None:
        selected_id = operation_id or self.current_id()
        if selected_id is None:
            return None
        with self.store.locked():
            record = self._require(selected_id)
            updated = replace(record, pid=str(pid), updated_at=utc_now())
            self.store.update_operation(updated)
            return updated

    def finish(
        self,
        outcome: OperationOutcome | str,
        *,
        operation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OperationRecord | None:
        selected_id = operation_id or self.current_id()
        if selected_id is None:
            return None
        with self.store.locked():
            record = self._require(selected_id)
            if record.state == OperationState.TERMINAL:
                return record
            selected_outcome = OperationOutcome(outcome)
            selected_metadata = dict(metadata or {})
            if (
                selected_outcome == OperationOutcome.SUCCEEDED
                and self._has_unknown_external_effect(selected_id)
            ):
                selected_outcome = OperationOutcome.UNKNOWN
                selected_metadata.setdefault(
                    "outcome_adjustment",
                    "succeeded_with_unknown_external_effect",
                )
            now = utc_now()
            updated = replace(
                record,
                state=OperationState.TERMINAL,
                outcome=selected_outcome,
                metadata={**record.metadata, **selected_metadata},
                updated_at=now,
                completed_at=now,
            )
            if not self.store.update_operation(
                updated,
                expected_states=[OperationState.RUNNING.value, OperationState.WAITING.value],
            ):
                return self._require(selected_id)
            return updated

    def wait(
        self,
        *,
        operation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OperationRecord | None:
        selected_id = operation_id or self.current_id()
        if selected_id is None:
            return None
        with self.store.locked():
            record = self._require(selected_id)
            if record.state == OperationState.TERMINAL:
                return record
            updated = replace(
                record,
                state=OperationState.WAITING,
                outcome=OperationOutcome.PENDING,
                metadata={**record.metadata, **dict(metadata or {})},
                updated_at=utc_now(),
                completed_at=None,
            )
            if not self.store.update_operation(
                updated,
                expected_states=[OperationState.RUNNING.value, OperationState.WAITING.value],
            ):
                return self._require(selected_id)
            return updated

    def link_evidence(
        self,
        evidence_type: str,
        evidence_id: str,
        role: OperationEvidenceRole | str,
        *,
        operation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> OperationEvidenceLink | None:
        selected_id = operation_id or self.current_id()
        if selected_id is None:
            return None
        if self.store.get_operation(selected_id) is None:
            return None
        link = OperationEvidenceLink(
            link_id=new_id("oplink"),
            operation_id=selected_id,
            evidence_type=str(evidence_type),
            evidence_id=str(evidence_id),
            role=str(role),
            created_at=utc_now(),
            metadata=dict(metadata or {}),
        )
        return link if self.store.insert_operation_evidence(link) else None

    def operation_for_evidence(self, evidence_types: Iterable[str], evidence_id: str) -> list[OperationRecord]:
        links = self.store.list_operation_evidence(
            evidence_types=list(evidence_types),
            evidence_id=str(evidence_id),
        )
        ids = sorted({link.operation_id for link in links})
        return [record for operation_id in ids if (record := self.store.get_operation(operation_id)) is not None]

    def interrupt_stale_running(self) -> list[str]:
        interrupted: list[str] = []
        for record in self.store.list_operations(state=OperationState.RUNNING.value):
            pending_effect = self._has_unknown_external_effect(record.operation_id)
            updated = self.finish(
                OperationOutcome.UNKNOWN if pending_effect else OperationOutcome.INTERRUPTED,
                operation_id=record.operation_id,
                metadata={
                    "recovery": (
                        "stale_running_with_pending_external_effect"
                        if pending_effect
                        else "stale_running_operation"
                    )
                },
            )
            if updated is not None and updated.outcome in {
                OperationOutcome.INTERRUPTED,
                OperationOutcome.UNKNOWN,
            }:
                interrupted.append(updated.operation_id)
        return interrupted

    @contextmanager
    def activate(self, operation_id: str) -> Iterator[OperationRecord]:
        record = self.resume(operation_id)
        token = self._set_current(record.operation_id)
        try:
            yield record
        finally:
            _CURRENT_OPERATION.reset(token)

    @contextmanager
    def attach(self, operation_id: str) -> Iterator[OperationRecord]:
        """Attach evidence to an operation without changing its lifecycle state."""
        record = self._require(operation_id)
        token = self._set_current(record.operation_id)
        try:
            yield record
        finally:
            _CURRENT_OPERATION.reset(token)

    @contextmanager
    def scope(
        self,
        *,
        kind: OperationKind | str,
        name: str,
        actor: str,
        pid: str | None,
        expected_roles: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        operation_id: str | None = None,
        parent_operation_id: str | None = None,
        auto_finish: bool = True,
    ) -> Iterator[OperationRecord]:
        record = (
            self.resume(operation_id)
            if operation_id is not None
            else self.start(
                kind=kind,
                name=name,
                actor=actor,
                pid=pid,
                parent_operation_id=parent_operation_id,
                expected_roles=expected_roles,
                metadata=metadata,
            )
        )
        token = self._set_current(record.operation_id)
        try:
            yield record
        except (HumanApprovalRequired, ProcessWaitRequired, ProcessMessageWaitRequired) as exc:
            self._record_wait(record.operation_id, exc)
            raise
        except (CapabilityDenied, PolicyDenied, ResourceLimitExceeded) as exc:
            self.finish(
                OperationOutcome.DENIED,
                operation_id=record.operation_id,
                metadata={"error_type": type(exc).__name__},
            )
            raise
        except asyncio.CancelledError:
            self.finish(OperationOutcome.INTERRUPTED, operation_id=record.operation_id)
            raise
        except BaseException as exc:
            self.finish(
                OperationOutcome.UNKNOWN if self._has_unknown_external_effect(record.operation_id) else OperationOutcome.FAILED,
                operation_id=record.operation_id,
                metadata={"error_type": type(exc).__name__},
            )
            raise
        else:
            if auto_finish:
                self.finish(OperationOutcome.SUCCEEDED, operation_id=record.operation_id)
        finally:
            _CURRENT_OPERATION.reset(token)

    def protected(
        self,
        *,
        kind: OperationKind | str,
        name: str,
        actor_arg: str = "pid",
        pid_arg: str = "pid",
        expected_roles: Iterable[str] = (),
        result_pid: bool = False,
    ) -> Callable[[F], F]:
        """Decorator for public boundaries whose exceptions determine outcome."""

        def decorate(function: F) -> F:
            signature = inspect.signature(function)

            def selected(bound: inspect.BoundArguments, key: str) -> str | None:
                value = bound.arguments.get(key)
                return str(value) if value is not None else None

            if inspect.iscoroutinefunction(function):
                @wraps(function)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    bound = signature.bind_partial(*args, **kwargs)
                    actor = selected(bound, actor_arg) or "runtime"
                    pid = selected(bound, pid_arg)
                    with self.scope(
                        kind=kind,
                        name=name,
                        actor=actor,
                        pid=pid,
                        expected_roles=expected_roles,
                    ) as operation:
                        result = await function(*args, **kwargs)
                        if result_pid and isinstance(result, str):
                            self.set_pid(result, operation_id=operation.operation_id)
                        return result

                return async_wrapper  # type: ignore[return-value]

            @wraps(function)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                bound = signature.bind_partial(*args, **kwargs)
                actor = selected(bound, actor_arg) or "runtime"
                pid = selected(bound, pid_arg)
                with self.scope(
                    kind=kind,
                    name=name,
                    actor=actor,
                    pid=pid,
                    expected_roles=expected_roles,
                ) as operation:
                    result = function(*args, **kwargs)
                    if result_pid and isinstance(result, str):
                        self.set_pid(result, operation_id=operation.operation_id)
                    return result

            return sync_wrapper  # type: ignore[return-value]

        return decorate

    def _record_wait(self, operation_id: str, exc: BaseException) -> None:
        metadata: dict[str, Any] = {"wait_type": type(exc).__name__}
        if isinstance(exc, HumanApprovalRequired):
            metadata["request_id"] = exc.request_id
            self.link_evidence(
                "human_request",
                exc.request_id,
                OperationEvidenceRole.WAIT,
                operation_id=operation_id,
            )
        elif isinstance(exc, ProcessWaitRequired):
            metadata["child_pid"] = exc.child_pid
            self.link_evidence(
                "process",
                exc.child_pid,
                OperationEvidenceRole.WAIT,
                operation_id=operation_id,
            )
        elif isinstance(exc, ProcessMessageWaitRequired):
            metadata["recipient_pid"] = exc.recipient_pid
        self.expect(OperationEvidenceRole.WAIT, operation_id=operation_id)
        self.wait(operation_id=operation_id, metadata=metadata)

    def _set_current(self, operation_id: str) -> Token[_CurrentOperation | None]:
        return _CURRENT_OPERATION.set(
            _CurrentOperation(manager_identity=self._identity, operation_id=operation_id)
        )

    def _require(self, operation_id: str) -> OperationRecord:
        record = self.store.get_operation(operation_id)
        if record is None:
            raise ValueError(f"operation not found: {operation_id}")
        return record

    def _has_unknown_external_effect(self, operation_id: str) -> bool:
        selected = self._require(operation_id)
        subtree = {operation_id}
        descendants = self.store.list_operations(root_operation_id=selected.root_operation_id)
        changed = True
        while changed:
            changed = False
            for candidate in descendants:
                if candidate.parent_operation_id in subtree and candidate.operation_id not in subtree:
                    subtree.add(candidate.operation_id)
                    changed = True
        links = self.store.list_operation_evidence(
            operation_ids=subtree,
            evidence_types=["external_effect"],
        )
        return any(
            effect is not None
            and (
                effect.effect_state == "pending"
                or effect.transaction_state == "unknown"
            )
            for link in links
            if (effect := self.store.get_external_effect(link.evidence_id)) is not None
        )

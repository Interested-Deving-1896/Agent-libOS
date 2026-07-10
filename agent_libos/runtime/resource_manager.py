from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from typing import Any

from agent_libos.models import AgentProcess, EventPriority, EventType, ProcessStatus, ResourceBudget, ResourceReservation, ResourceUsage
from agent_libos.models.exceptions import NotFound, ResourceLimitExceeded, ValidationError
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.storage import RuntimeStore
from agent_libos.utils.ids import utc_now
from agent_libos.utils.serde import to_jsonable


_TERMINAL_STATUSES = {ProcessStatus.EXITED, ProcessStatus.FAILED, ProcessStatus.KILLED}
_USAGE_FIELD_NAMES = {field.name for field in fields(ResourceUsage)}

# Most counters are cumulative. Peak RSS is different: one large subprocess
# should not make future small subprocesses impossible, but it must still be
# compared against the configured per-process tree memory ceiling.
_PEAK_USAGE_FIELDS = {"subprocess_peak_memory_bytes"}

_BUDGET_USAGE_MAP: dict[str, tuple[str, ...]] = {
    "max_tool_calls": ("tool_calls",),
    "max_child_processes": ("child_processes",),
    "max_runtime_seconds": ("runtime_seconds",),
    "max_context_materialization_total_tokens": ("context_materialized_tokens",),
    "max_llm_calls": ("llm_calls",),
    "max_llm_total_tokens": ("llm_total_tokens",),
    "max_subprocess_wall_seconds": ("subprocess_wall_seconds",),
    "max_subprocess_cpu_seconds": ("subprocess_cpu_seconds",),
    "max_subprocess_memory_bytes": ("subprocess_peak_memory_bytes",),
    "max_external_read_bytes": ("external_read_bytes",),
    "max_external_write_bytes": ("external_write_bytes",),
    "max_jsonrpc_bytes": ("jsonrpc_request_bytes", "jsonrpc_response_bytes"),
    "max_mcp_bytes": ("mcp_request_bytes", "mcp_response_bytes"),
    "max_deno_syscalls": ("deno_syscalls",),
}

_NON_RESERVABLE_BUDGET_FIELDS = {"max_subprocess_memory_bytes", "max_child_processes"}


class ResourceManager:
    """Hierarchical process resource accounting.

    Capability grants decide whether a process may attempt an operation.
    Resource budgets decide whether the process tree still has enough quota to
    spend on that operation. Charging walks the pid -> parent chain so a parent
    can bound the total consumption of all descendants.
    """

    def __init__(self, store: RuntimeStore, audit: AuditManager, events: EventBus) -> None:
        self.store = store
        self.audit = audit
        self.events = events
        self._process_kill_finalizer: Callable[..., None] | None = None
        self._object_task_terminal_notifier: Callable[[str], None] | None = None

    def bind_process_kill_finalizer(self, finalizer: Callable[..., None]) -> None:
        self._process_kill_finalizer = finalizer

    def bind_object_task_terminal_notifier(self, notifier: Callable[[str], None]) -> None:
        self._object_task_terminal_notifier = notifier

    def preflight(
        self,
        pid: str,
        request: ResourceUsage | dict[str, Any],
        *,
        source: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        usage = self._coerce_usage(request)
        if self._is_zero(usage):
            return
        with self.store.locked():
            self._preflight_locked(pid, usage, source=source, context=context)

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
        delta = self._coerce_usage(usage)
        if self._is_zero(delta):
            return
        exceeded_after_charge: tuple[AgentProcess, dict[str, Any]] | None = None
        # A hierarchical charge is one accounting mutation.  In particular,
        # never leave the child charged when an ancestor update, reservation
        # consume, event, or audit write fails part-way through the chain.
        with self.store.transaction():
            chain = self._process_chain(pid)
            relevant_fields = self._nonzero_fields(delta)
            if not allow_overage:
                self._preflight_locked(pid, delta, source=source, context=context)
            for index, process in enumerate(chain):
                latest = self._get(process.pid)
                latest.resource_usage = self._merge_usage(latest.resource_usage, delta)
                latest.updated_at = utc_now()
                self.store.update_process(latest)
                if index > 0:
                    self._consume_reservation_locked(latest.pid, chain[index - 1].pid, delta, relevant_fields)
                exceeded = self._first_exceeded_effective(
                    latest,
                    relevant_fields=relevant_fields,
                )
                if exceeded is not None and exceeded_after_charge is None:
                    exceeded_after_charge = (latest, exceeded)
            self.events.emit(
                EventType.RESOURCE_CHARGED,
                source=source,
                target=pid,
                payload={"pid": pid, "usage": to_jsonable(delta), "context": context or {}},
            )
            self.audit.record(
                actor=pid,
                action="resource.charge",
                target=f"process:{pid}",
                decision={
                    "source": source,
                    "usage": to_jsonable(delta),
                    "charged_pids": [process.pid for process in chain],
                    "context": context or {},
                },
            )
        if exceeded_after_charge is None:
            return
        owner, exceeded = exceeded_after_charge
        message = self._limit_message(owner.pid, exceeded)
        # Terminal hooks acquire Human/Object Memory locks.  Invoke the kill
        # path only after releasing the accounting transaction's store lock.
        if kill_on_exceed:
            self.kill_if_exceeded(owner.pid, reason=message, owner_pid=owner.pid, limit=exceeded)
        raise ResourceLimitExceeded(message)

    def kill_if_exceeded(
        self,
        pid: str,
        *,
        reason: str,
        owner_pid: str | None = None,
        limit: dict[str, Any] | None = None,
    ) -> None:
        killed: list[str] = []
        # Persist the complete descendant state transition, reservation
        # release, and corresponding evidence atomically.  Cross-subsystem
        # terminal hooks run only after this transaction releases the store
        # lock, avoiding a store -> terminal-lock / terminal-lock -> store
        # inversion with HumanRequestManager.
        with self.store.transaction():
            for process in self._descendant_tree(pid):
                if process.status in _TERMINAL_STATUSES:
                    continue
                process.status = ProcessStatus.KILLED
                process.status_message = reason
                process.updated_at = utc_now()
                self.store.update_process(process)
                self.store.delete_resource_reservations_for_process(process.pid)
                killed.append(process.pid)
            self.events.emit(
                EventType.RESOURCE_LIMIT_EXCEEDED,
                source="resource_manager",
                target=pid,
                priority=EventPriority.CRITICAL,
                payload={"pid": pid, "owner_pid": owner_pid or pid, "reason": reason, "killed_pids": killed, "limit": limit or {}},
            )
            self.audit.record(
                actor="resource_manager",
                action="resource.limit_exceeded",
                target=f"process:{pid}",
                decision={"reason": reason, "owner_pid": owner_pid or pid, "killed_pids": killed, "limit": limit or {}},
            )
        finalizer_errors: list[dict[str, str]] = []
        for killed_pid in killed:
            try:
                self._wake_parent_waiting_on_child(killed_pid)
            except Exception as exc:
                finalizer_errors.append(
                    {"phase": "wake_parent", "pid": killed_pid, "error": f"{type(exc).__name__}: {exc}"}
                )
            try:
                self._notify_object_task_process_terminal(killed_pid)
            except Exception as exc:
                finalizer_errors.append(
                    {"phase": "terminal_notify", "pid": killed_pid, "error": f"{type(exc).__name__}: {exc}"}
                )
        if killed and self._process_kill_finalizer is not None:
            try:
                self._process_kill_finalizer(killed, reason=reason)
            except Exception as exc:
                finalizer_errors.append(
                    {"phase": "process_finalize", "pid": pid, "error": f"{type(exc).__name__}: {exc}"}
                )
        if finalizer_errors:
            try:
                self.audit.record(
                    actor="resource_manager",
                    action="resource.limit_finalize_failed",
                    target=f"process:{pid}",
                    decision={"reason": reason, "errors": finalizer_errors},
                )
            except Exception:
                # The terminal state is already committed.  Do not replace the
                # caller's ResourceLimitExceeded with a secondary warning-sink
                # failure or skip the remaining cleanup callbacks.
                pass

    def _wake_parent_waiting_on_child(self, child_pid: str) -> None:
        with self.store.transaction():
            child = self.store.get_process(child_pid)
            if child is None or child.parent_pid is None:
                return
            parent = self.store.get_process(child.parent_pid)
            if parent is None:
                return
            if parent.status != ProcessStatus.WAITING_EVENT:
                return
            if parent.status_message != f"waiting for {child.pid}":
                return
            parent.status = ProcessStatus.RUNNABLE
            parent.status_message = None
            parent.updated_at = utc_now()
            self.store.update_process(parent)
            self.audit.record(
                actor="resource_manager",
                action="process.wait_wake",
                target=f"process:{parent.pid}",
                decision={"child": child.pid, "child_status": child.status.value},
            )

    def _notify_object_task_process_terminal(self, pid: str) -> None:
        if self._object_task_terminal_notifier is None:
            return
        self._object_task_terminal_notifier(pid)

    def has_limit(self, pid: str, budget_field: str) -> bool:
        if budget_field not in _BUDGET_USAGE_MAP:
            raise ValidationError(f"unknown resource budget field: {budget_field}")
        return any(getattr(process.resource_budget, budget_field) is not None for process in self._process_chain(pid))

    def remaining_cumulative(self, pid: str, budget_field: str, usage_field: str) -> float | None:
        if budget_field not in _BUDGET_USAGE_MAP or usage_field not in _USAGE_FIELD_NAMES:
            raise ValidationError(f"unknown resource remaining query: {budget_field}/{usage_field}")
        with self.store.locked():
            return self._remaining_budget_field_locked(pid, budget_field, (usage_field,))

    def peak_limit(self, pid: str, budget_field: str) -> int | None:
        if budget_field not in _BUDGET_USAGE_MAP:
            raise ValidationError(f"unknown resource budget field: {budget_field}")
        selected: int | None = None
        for process in self._process_chain(pid):
            limit = getattr(process.resource_budget, budget_field)
            if limit is None:
                continue
            value = int(limit)
            selected = value if selected is None else min(selected, value)
        return selected

    def validate_child_budget(
        self,
        parent_pid: str,
        child_budget: ResourceBudget,
        *,
        reserved_usage: ResourceUsage | None = None,
    ) -> None:
        reserve = reserved_usage or ResourceUsage()
        self._coerce_usage(reserve)
        with self.store.locked():
            if not self._is_zero(reserve):
                self._preflight_locked(
                    parent_pid,
                    reserve,
                    source="resource.validate_child_budget",
                    context={"child_budget": to_jsonable(child_budget)},
                )
            parent_window = self.context_materialization_window_limit(parent_pid)
            if child_budget.max_context_materialization_tokens > parent_window:
                raise ResourceLimitExceeded(
                    "child budget max_context_materialization_tokens="
                    f"{child_budget.max_context_materialization_tokens} exceeds parent limit {parent_window}"
                )
            for budget_field, usage_fields in _BUDGET_USAGE_MAP.items():
                requested = getattr(child_budget, budget_field)
                if requested is None:
                    continue
                if budget_field in _NON_RESERVABLE_BUDGET_FIELDS:
                    limit = self.peak_limit(parent_pid, budget_field)
                    if limit is not None and float(requested) > float(limit):
                        raise ResourceLimitExceeded(
                            f"child budget {budget_field}={requested} exceeds parent limit {limit}"
                        )
                    continue
                remaining = self._remaining_budget_field_locked(
                    parent_pid,
                    budget_field,
                    usage_fields,
                    reserved_usage=reserve,
                )
                if remaining is None:
                    continue
                if float(requested) > remaining:
                    raise ResourceLimitExceeded(
                        f"child budget {budget_field}={requested} exceeds parent remaining {remaining:g}"
                    )

    def remaining_budget(self, pid: str) -> ResourceBudget:
        with self.store.locked():
            values: dict[str, Any] = {
                "max_context_materialization_tokens": self.context_materialization_window_limit(pid),
            }
            for budget_field, usage_fields in _BUDGET_USAGE_MAP.items():
                if budget_field == "max_subprocess_memory_bytes":
                    values[budget_field] = self.peak_limit(pid, budget_field)
                    continue
                remaining = self._remaining_budget_field_locked(pid, budget_field, usage_fields)
                if remaining is None:
                    values[budget_field] = None
                else:
                    values[budget_field] = int(remaining) if remaining.is_integer() else remaining
            return ResourceBudget(**values)

    def reserve_child_budget(self, parent_pid: str, child_pid: str, child_budget: ResourceBudget) -> None:
        with self.store.locked():
            self.validate_child_budget(parent_pid, child_budget, reserved_usage=ResourceUsage(child_processes=1))
            reserved = self._reservation_from_budget(child_budget)
            if not reserved:
                return
            now = utc_now()
            self.store.upsert_resource_reservation(
                ResourceReservation(
                    parent_pid=parent_pid,
                    child_pid=child_pid,
                    reserved=reserved,
                    created_at=now,
                    updated_at=now,
                )
            )
            self.audit.record(
                actor=parent_pid,
                action="resource.reserve_child_budget",
                target=f"process:{child_pid}",
                decision={"reserved": reserved},
            )

    def release_process_reservations(self, pid: str) -> None:
        with self.store.locked():
            reservations = self.store.list_resource_reservations(child_pid=pid)
            reservations.extend(self.store.list_resource_reservations(parent_pid=pid))
            self.store.delete_resource_reservations_for_process(pid)
            if reservations:
                self.audit.record(
                    actor="resource_manager",
                    action="resource.release_process_reservations",
                    target=f"process:{pid}",
                    decision={
                        "reservations": [
                            {
                                "parent_pid": item.parent_pid,
                                "child_pid": item.child_pid,
                                "reserved": item.reserved,
                            }
                            for item in reservations
                        ]
                    },
                )

    def context_materialization_window_limit(self, pid: str) -> int:
        selected: int | None = None
        for process in self._process_chain(pid):
            value = int(process.resource_budget.max_context_materialization_tokens)
            selected = value if selected is None else min(selected, value)
        return selected if selected is not None else 0

    def _get(self, pid: str) -> AgentProcess:
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        return process

    def _preflight_locked(
        self,
        pid: str,
        usage: ResourceUsage,
        *,
        source: str,
        context: dict[str, Any] | None,
    ) -> None:
        relevant_fields = self._nonzero_fields(usage)
        chain = self._process_chain(pid)
        for index, process in enumerate(chain):
            candidate = self._merge_usage(process.resource_usage, usage)
            consuming_child_pid = chain[index - 1].pid if index > 0 else None
            exceeded = self._first_exceeded_effective(
                process,
                usage=candidate,
                relevant_fields=relevant_fields,
                consuming_child_pid=consuming_child_pid,
                consuming_usage=usage,
            )
            if exceeded is None:
                continue
            message = self._limit_message(process.pid, exceeded)
            self.audit.record(
                actor=pid,
                action="resource.preflight_denied",
                target=f"process:{process.pid}",
                decision={
                    "source": source,
                    "context": context or {},
                    "requested_usage": to_jsonable(usage),
                    "limit": exceeded,
                    "message": message,
                },
            )
            raise ResourceLimitExceeded(message)

    def _remaining_budget_field_locked(
        self,
        pid: str,
        budget_field: str,
        usage_fields: tuple[str, ...],
        *,
        reserved_usage: ResourceUsage | None = None,
    ) -> float | None:
        remaining: float | None = None
        for process in self._process_chain(pid):
            limit = getattr(process.resource_budget, budget_field)
            if limit is None:
                continue
            usage = (
                self._merge_usage(process.resource_usage, reserved_usage)
                if reserved_usage is not None
                else process.resource_usage
            )
            value = sum(float(getattr(usage, usage_field)) for usage_field in usage_fields)
            if budget_field not in _NON_RESERVABLE_BUDGET_FIELDS:
                value += self._reserved_budget_value_locked(process.pid, budget_field)
            process_remaining = max(0.0, float(limit) - value)
            remaining = process_remaining if remaining is None else min(remaining, process_remaining)
        return remaining

    def _process_chain(self, pid: str) -> list[AgentProcess]:
        chain: list[AgentProcess] = []
        current = self._get(pid)
        while True:
            chain.append(current)
            if current.parent_pid is None:
                return chain
            current = self._get(current.parent_pid)

    def _descendant_tree(self, pid: str) -> list[AgentProcess]:
        selected: list[AgentProcess] = []
        stack = [self._get(pid)]
        while stack:
            process = stack.pop()
            selected.append(process)
            stack.extend(self.store.list_child_processes(process.pid))
        return selected

    def _reservation_from_budget(self, budget: ResourceBudget) -> dict[str, float]:
        reserved: dict[str, float] = {}
        for budget_field in _BUDGET_USAGE_MAP:
            if budget_field in _NON_RESERVABLE_BUDGET_FIELDS:
                continue
            value = getattr(budget, budget_field)
            if value is None:
                continue
            reserved[budget_field] = float(value)
        return reserved

    def _consume_reservation_locked(
        self,
        parent_pid: str,
        child_pid: str,
        usage: ResourceUsage,
        relevant_fields: set[str],
    ) -> None:
        reservation = self.store.get_resource_reservation(parent_pid, child_pid)
        if reservation is None:
            return
        changed = False
        remaining = dict(reservation.reserved)
        for budget_field, usage_fields in _BUDGET_USAGE_MAP.items():
            if budget_field in _NON_RESERVABLE_BUDGET_FIELDS:
                continue
            if not (set(usage_fields) & relevant_fields):
                continue
            current = float(remaining.get(budget_field, 0.0))
            if current <= 0:
                continue
            consumed = min(current, self._usage_value(usage, budget_field, usage_fields))
            if consumed <= 0:
                continue
            next_value = current - consumed
            if next_value <= 0:
                remaining.pop(budget_field, None)
            else:
                remaining[budget_field] = next_value
            changed = True
        if not changed:
            return
        if remaining:
            self.store.upsert_resource_reservation(
                ResourceReservation(
                    parent_pid=parent_pid,
                    child_pid=child_pid,
                    reserved=remaining,
                    created_at=reservation.created_at,
                    updated_at=utc_now(),
                )
            )
        else:
            self.store.delete_resource_reservation(parent_pid, child_pid)

    def _reserved_budget_value_locked(
        self,
        parent_pid: str,
        budget_field: str,
        *,
        consuming_child_pid: str | None = None,
        consuming_usage: ResourceUsage | None = None,
    ) -> float:
        total = 0.0
        usage_fields = _BUDGET_USAGE_MAP[budget_field]
        for reservation in self.store.list_resource_reservations(parent_pid=parent_pid):
            value = float(reservation.reserved.get(budget_field, 0.0))
            if value <= 0:
                continue
            if consuming_child_pid == reservation.child_pid and consuming_usage is not None:
                value = max(0.0, value - self._usage_value(consuming_usage, budget_field, usage_fields))
            total += value
        return total

    def _usage_value(
        self,
        usage: ResourceUsage,
        budget_field: str,
        usage_fields: tuple[str, ...],
    ) -> float:
        if budget_field == "max_subprocess_memory_bytes":
            return float(getattr(usage, "subprocess_peak_memory_bytes"))
        return sum(float(getattr(usage, usage_field)) for usage_field in usage_fields)

    def _coerce_usage(self, usage: ResourceUsage | dict[str, Any]) -> ResourceUsage:
        if isinstance(usage, ResourceUsage):
            self._validate_usage(usage)
            return usage
        unknown = sorted(set(usage) - _USAGE_FIELD_NAMES)
        if unknown:
            raise ValidationError(f"unknown resource usage fields: {unknown}")
        try:
            coerced = ResourceUsage(**dict(usage))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        self._validate_usage(coerced)
        return coerced

    def _merge_usage(self, current: ResourceUsage, delta: ResourceUsage) -> ResourceUsage:
        values: dict[str, Any] = {}
        for field in fields(ResourceUsage):
            name = field.name
            current_value = getattr(current, name)
            delta_value = getattr(delta, name)
            if name in _PEAK_USAGE_FIELDS:
                values[name] = max(current_value, delta_value)
            else:
                values[name] = current_value + delta_value
        return ResourceUsage(**values)

    def _first_exceeded(
        self,
        budget: ResourceBudget,
        usage: ResourceUsage,
        *,
        relevant_fields: set[str] | None = None,
    ) -> dict[str, Any] | None:
        for budget_field, usage_fields in _BUDGET_USAGE_MAP.items():
            if relevant_fields is not None and not (set(usage_fields) & relevant_fields):
                continue
            limit = getattr(budget, budget_field)
            if limit is None:
                continue
            if budget_field == "max_subprocess_memory_bytes":
                value = getattr(usage, "subprocess_peak_memory_bytes")
            else:
                value = sum(getattr(usage, usage_field) for usage_field in usage_fields)
            if float(value) > float(limit):
                return {"budget": budget_field, "usage": list(usage_fields), "value": value, "limit": limit}
        return None

    def _first_exceeded_effective(
        self,
        process: AgentProcess,
        *,
        usage: ResourceUsage | None = None,
        relevant_fields: set[str] | None = None,
        consuming_child_pid: str | None = None,
        consuming_usage: ResourceUsage | None = None,
    ) -> dict[str, Any] | None:
        selected_usage = usage or process.resource_usage
        for budget_field, usage_fields in _BUDGET_USAGE_MAP.items():
            if relevant_fields is not None and not (set(usage_fields) & relevant_fields):
                continue
            limit = getattr(process.resource_budget, budget_field)
            if limit is None:
                continue
            value = self._usage_value(selected_usage, budget_field, usage_fields)
            if budget_field not in _NON_RESERVABLE_BUDGET_FIELDS:
                value += self._reserved_budget_value_locked(
                    process.pid,
                    budget_field,
                    consuming_child_pid=consuming_child_pid,
                    consuming_usage=consuming_usage,
                )
            if float(value) > float(limit):
                return {"budget": budget_field, "usage": list(usage_fields), "value": value, "limit": limit}
        return None

    def _is_zero(self, usage: ResourceUsage) -> bool:
        return all(getattr(usage, name) == 0 for name in _USAGE_FIELD_NAMES)

    def _nonzero_fields(self, usage: ResourceUsage) -> set[str]:
        return {name for name in _USAGE_FIELD_NAMES if getattr(usage, name) != 0}

    def _validate_usage(self, usage: ResourceUsage) -> None:
        try:
            usage.validate()
        except ValueError as exc:
            raise ValidationError(f"resource usage {exc}") from exc

    def _limit_message(self, pid: str, exceeded: dict[str, Any]) -> str:
        if exceeded["budget"] == "max_child_processes":
            return (
                f"process {pid} exhausted child process budget: "
                f"{exceeded['value']}/{exceeded['limit']}"
            )
        return (
            f"process {pid} exceeded {exceeded['budget']}: "
            f"{exceeded['value']} > {exceeded['limit']}"
        )

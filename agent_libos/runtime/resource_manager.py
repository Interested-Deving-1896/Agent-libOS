from __future__ import annotations

from dataclasses import fields
from typing import Any

from agent_libos.models import AgentProcess, EventPriority, EventType, ProcessStatus, ResourceBudget, ResourceUsage
from agent_libos.models.exceptions import NotFound, ResourceLimitExceeded, ValidationError
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.storage import SQLiteStore
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
    "max_llm_calls": ("llm_calls",),
    "max_llm_total_tokens": ("llm_total_tokens",),
    "max_subprocess_wall_seconds": ("subprocess_wall_seconds",),
    "max_subprocess_cpu_seconds": ("subprocess_cpu_seconds",),
    "max_subprocess_memory_bytes": ("subprocess_peak_memory_bytes",),
    "max_external_read_bytes": ("external_read_bytes",),
    "max_external_write_bytes": ("external_write_bytes",),
    "max_jsonrpc_bytes": ("jsonrpc_request_bytes", "jsonrpc_response_bytes"),
    "max_deno_syscalls": ("deno_syscalls",),
}


class ResourceManager:
    """Hierarchical process resource accounting.

    Capability grants decide whether a process may attempt an operation.
    Resource budgets decide whether the process tree still has enough quota to
    spend on that operation. Charging walks the pid -> parent chain so a parent
    can bound the total consumption of all descendants.
    """

    def __init__(self, store: SQLiteStore, audit: AuditManager, events: EventBus) -> None:
        self.store = store
        self.audit = audit
        self.events = events

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
        with self.store._lock:
            relevant_fields = self._nonzero_fields(usage)
            for process in self._process_chain(pid):
                candidate = self._merge_usage(process.resource_usage, usage)
                exceeded = self._first_exceeded(process.resource_budget, candidate, relevant_fields=relevant_fields)
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
        with self.store._lock:
            chain = self._process_chain(pid)
            relevant_fields = self._nonzero_fields(delta)
            if not allow_overage:
                self._preflight_locked(pid, delta, source=source, context=context)
            exceeded_after_charge: tuple[AgentProcess, dict[str, Any]] | None = None
            for process in chain:
                latest = self._get(process.pid)
                latest.resource_usage = self._merge_usage(latest.resource_usage, delta)
                latest.updated_at = utc_now()
                self.store.update_process(latest)
                exceeded = self._first_exceeded(latest.resource_budget, latest.resource_usage, relevant_fields=relevant_fields)
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
        with self.store._lock:
            killed: list[str] = []
            for process in self._descendant_tree(pid):
                if process.status in _TERMINAL_STATUSES:
                    continue
                process.status = ProcessStatus.KILLED
                process.status_message = reason
                process.updated_at = utc_now()
                self.store.update_process(process)
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

    def has_limit(self, pid: str, budget_field: str) -> bool:
        if budget_field not in _BUDGET_USAGE_MAP:
            raise ValidationError(f"unknown resource budget field: {budget_field}")
        return any(getattr(process.resource_budget, budget_field) is not None for process in self._process_chain(pid))

    def remaining_cumulative(self, pid: str, budget_field: str, usage_field: str) -> float | None:
        if budget_field not in _BUDGET_USAGE_MAP or usage_field not in _USAGE_FIELD_NAMES:
            raise ValidationError(f"unknown resource remaining query: {budget_field}/{usage_field}")
        with self.store._lock:
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
        with self.store._lock:
            parent = self._get(parent_pid)
            for budget_field, usage_fields in _BUDGET_USAGE_MAP.items():
                requested = getattr(child_budget, budget_field)
                if requested is None:
                    continue
                if budget_field in {"max_subprocess_memory_bytes", "max_child_processes"}:
                    limit = getattr(parent.resource_budget, budget_field)
                    if limit is not None and requested > limit:
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
        with self.store._lock:
            process = self._get(pid)
            values: dict[str, Any] = {"max_materialized_tokens": process.resource_budget.max_materialized_tokens}
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
        for process in self._process_chain(pid):
            candidate = self._merge_usage(process.resource_usage, usage)
            exceeded = self._first_exceeded(process.resource_budget, candidate, relevant_fields=relevant_fields)
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
        processes = self.store.list_processes()
        by_parent: dict[str | None, list[AgentProcess]] = {}
        for process in processes:
            by_parent.setdefault(process.parent_pid, []).append(process)
        selected: list[AgentProcess] = []
        stack = [self._get(pid)]
        while stack:
            process = stack.pop()
            selected.append(process)
            stack.extend(by_parent.get(process.pid, []))
        return selected

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

    def _is_zero(self, usage: ResourceUsage) -> bool:
        return all(getattr(usage, name) == 0 for name in _USAGE_FIELD_NAMES)

    def _nonzero_fields(self, usage: ResourceUsage) -> set[str]:
        return {name for name in _USAGE_FIELD_NAMES if getattr(usage, name) != 0}

    def _validate_usage(self, usage: ResourceUsage) -> None:
        for name in _USAGE_FIELD_NAMES:
            value = getattr(usage, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"resource usage {name} must be numeric")
            if value < 0:
                raise ValidationError(f"resource usage {name} cannot be negative")

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

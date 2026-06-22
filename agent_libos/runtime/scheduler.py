from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.utils.ids import utc_now
from agent_libos.models import ProcessStatus, ResourceUsage
from agent_libos.models.exceptions import ResourceLimitExceeded
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.storage import SQLiteStore


Quantum = Callable[[str], Any | Awaitable[Any]]
_SCHEDULER_DEFAULTS = DEFAULT_CONFIG.scheduler


class AsyncProcessScheduler:
    """Cooperative async scheduler for AgentProcess quanta."""

    TERMINAL_STATUSES = {ProcessStatus.EXITED, ProcessStatus.FAILED, ProcessStatus.KILLED}

    def __init__(
        self,
        store: SQLiteStore,
        audit: AuditManager,
        poll_interval_s: float = _SCHEDULER_DEFAULTS.poll_interval_s,
        resources: Any | None = None,
    ):
        self.store = store
        self.audit = audit
        self.poll_interval_s = poll_interval_s
        self.resources = resources

    def next_runnable(self) -> str | None:
        runnable = self.store.list_processes_by_status(ProcessStatus.RUNNABLE)
        return runnable[0].pid if runnable else None

    def runnable_pids(self) -> list[str]:
        return [proc.pid for proc in self.store.list_processes_by_status(ProcessStatus.RUNNABLE)]

    async def arun_once(self, quantum: Quantum) -> Any:
        pid = self.next_runnable()
        if pid is None:
            return None
        return await self._run_quantum(pid, quantum)

    def run_once(self, quantum: Quantum) -> Any:
        return _run_sync(self.arun_once(quantum))

    async def arun_until_idle(self, quantum: Quantum, max_quanta: int | None = _SCHEDULER_DEFAULTS.max_quanta) -> list[Any]:
        results: list[Any] = []
        tasks: dict[str, asyncio.Task[list[Any]]] = {}
        quanta_used = 0
        effective_max_quanta = max_quanta
        unblock_quanta_used = 0
        unblock_quanta_limit = max(1, max_quanta or 0) if max_quanta is not None else None
        drain_polls_used = 0
        drain_poll_limit = max(1, int(0.5 / max(self.poll_interval_s, 0.001))) if max_quanta is not None else None
        quanta_lock = asyncio.Lock()

        async def reserve_quantum() -> bool:
            # The quantum budget is global across process tasks, not per process.
            nonlocal quanta_used
            async with quanta_lock:
                if _budget_exhausted(quanta_used, effective_max_quanta):
                    return False
                quanta_used += 1
                return True

        async def process_loop(pid: str) -> list[Any]:
            process_results: list[Any] = []
            while await reserve_quantum():
                process = self.store.get_process(pid)
                if process is None or process.status != ProcessStatus.RUNNABLE:
                    break
                try:
                    process_results.append(await self._run_quantum(pid, quantum))
                except Exception as exc:
                    self._fail_process_task(pid, exc)
                    process_results.append({"ok": False, "pid": pid, "error": str(exc)})
                    break
                latest = self.store.get_process(pid)
                if latest is None or latest.status != ProcessStatus.RUNNABLE:
                    break
                # Yield so a sleeping or long-running async tool in another
                # process can advance without this pid monopolizing the loop.
                await asyncio.sleep(0)
            return process_results

        while True:
            # Start one task per runnable pid. Each task keeps advancing its own
            # process until it blocks, exits, fails, or the shared budget is used.
            for pid in self.runnable_pids():
                if _budget_exhausted(quanta_used, effective_max_quanta):
                    break
                if pid not in tasks:
                    tasks[pid] = asyncio.create_task(process_loop(pid), name=f"agent-process:{pid}")

            if not tasks:
                break

            done, _pending = await asyncio.wait(
                tasks.values(),
                timeout=self.poll_interval_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                pid = self._pid_for_task(tasks, task)
                if pid is not None:
                    tasks.pop(pid, None)
                try:
                    results.extend(task.result())
                except asyncio.CancelledError:
                    if pid is not None:
                        self._record_task_cancelled(pid, reason="cancelled")
                except Exception as exc:
                    if pid is not None:
                        self._fail_process_task(pid, exc)
                    results.append({"ok": False, "pid": pid, "error": str(exc)})

            if _budget_exhausted(quanta_used, effective_max_quanta) and not done:
                runnable_dependencies = [pid for pid in self.runnable_pids() if pid not in tasks]
                if (
                    runnable_dependencies
                    and unblock_quanta_limit is not None
                    and unblock_quanta_used < unblock_quanta_limit
                ):
                    # A bounded run may have spent its nominal budget inside a
                    # parent quantum that is waiting for a child/message. Grant
                    # limited dependency quanta so the waiter can be unblocked.
                    unblock_quanta_used += 1
                    effective_max_quanta = (effective_max_quanta or 0) + 1
                    self.audit.record(
                        actor="scheduler",
                        action="scheduler.unblock_quantum_reserved",
                        target="scheduler",
                        decision={
                            "quanta_used": quanta_used,
                            "max_quanta": max_quanta,
                            "unblock_quanta_used": unblock_quanta_used,
                            "runnable_dependencies": runnable_dependencies,
                        },
                    )
                    continue
                if (
                    drain_poll_limit is not None
                    and drain_polls_used < drain_poll_limit
                    and self._has_running_pending_task(tasks)
                ):
                    drain_polls_used += 1
                    continue
                await self._cancel_pending_tasks(tasks, results, reason="max_quanta_exhausted")
                break

        return results

    def run_until_idle(self, quantum: Quantum, max_quanta: int | None = _SCHEDULER_DEFAULTS.max_quanta) -> list[Any]:
        return _run_sync(self.arun_until_idle(quantum, max_quanta=max_quanta))

    async def arun_pid_until_idle(
        self,
        pid: str,
        quantum: Quantum,
        max_quanta: int | None = _SCHEDULER_DEFAULTS.max_quanta,
    ) -> list[Any]:
        """Advance one process until it blocks, exits, fails, or exhausts budget."""
        results: list[Any] = []
        quanta_used = 0
        while not _budget_exhausted(quanta_used, max_quanta):
            process = self.store.get_process(pid)
            if process is None or process.status != ProcessStatus.RUNNABLE:
                break
            try:
                quanta_used += 1
                results.append(await self._run_quantum(pid, quantum))
            except Exception as exc:
                self._fail_process_task(pid, exc)
                results.append({"ok": False, "pid": pid, "error": str(exc)})
                break
            latest = self.store.get_process(pid)
            if latest is None or latest.status != ProcessStatus.RUNNABLE:
                break
            await asyncio.sleep(0)
        return results

    def run_pid_until_idle(
        self,
        pid: str,
        quantum: Quantum,
        max_quanta: int | None = _SCHEDULER_DEFAULTS.max_quanta,
    ) -> list[Any]:
        return _run_sync(self.arun_pid_until_idle(pid, quantum, max_quanta=max_quanta))

    async def _run_quantum(self, pid: str, quantum: Quantum) -> Any:
        process = self.store.get_process(pid)
        if process is None or process.status != ProcessStatus.RUNNABLE:
            return None
        process.status = ProcessStatus.RUNNING
        process.updated_at = utc_now()
        self.store.update_process(process)
        self.audit.record(actor="scheduler", action="scheduler.run_quantum", target=f"process:{pid}")
        started_at = time.perf_counter()
        result: Any = None
        error: BaseException | None = None
        resource_error: ResourceLimitExceeded | None = None
        try:
            result = quantum(pid)
            if inspect.isawaitable(result):
                result = await result
        except BaseException as exc:
            error = exc
        finally:
            if self.resources is not None:
                elapsed = max(0.0, time.perf_counter() - started_at)
                try:
                    self.resources.charge(
                        pid,
                        ResourceUsage(runtime_seconds=elapsed),
                        source="scheduler.quantum",
                        context={"elapsed_s": elapsed},
                        allow_overage=True,
                        kill_on_exceed=True,
                    )
                except ResourceLimitExceeded as exc:
                    resource_error = exc
            latest = self.store.get_process(pid)
            # A primitive may deliberately set WAITING_HUMAN, EXITED, or another
            # status during the quantum. Only restore RUNNABLE for plain returns.
            if latest is not None and latest.status == ProcessStatus.RUNNING:
                latest.status = ProcessStatus.RUNNABLE
                latest.updated_at = utc_now()
                self.store.update_process(latest)
        if error is not None:
            raise error
        if resource_error is not None:
            raise resource_error
        return result

    def _fail_process_task(self, pid: str, exc: Exception) -> None:
        process = self.store.get_process(pid)
        if process is not None and process.status not in self.TERMINAL_STATUSES:
            process.status = ProcessStatus.FAILED
            process.status_message = f"scheduler task failed: {exc}"
            process.updated_at = utc_now()
            self.store.update_process(process)
        self.audit.record(
            actor="scheduler",
            action="scheduler.process_task_failed",
            target=f"process:{pid}",
            decision={"error": str(exc), "error_type": type(exc).__name__},
        )

    async def _cancel_pending_tasks(
        self,
        tasks: dict[str, asyncio.Task[list[Any]]],
        results: list[Any],
        *,
        reason: str,
    ) -> None:
        pending = list(tasks.items())
        for _pid, task in pending:
            task.cancel()
        outcomes = await asyncio.gather(*(task for _pid, task in pending), return_exceptions=True)
        for (pid, _task), outcome in zip(pending, outcomes):
            tasks.pop(pid, None)
            if isinstance(outcome, list):
                results.extend(outcome)
            elif isinstance(outcome, asyncio.CancelledError):
                self._record_task_cancelled(pid, reason=reason)
            elif isinstance(outcome, Exception):
                self._fail_process_task(pid, outcome)
                results.append({"ok": False, "pid": pid, "error": str(outcome)})
            else:
                results.append(outcome)

    def _record_task_cancelled(self, pid: str, *, reason: str) -> None:
        self.audit.record(
            actor="scheduler",
            action="scheduler.process_task_cancelled",
            target=f"process:{pid}",
            decision={"reason": reason},
        )

    def _has_running_pending_task(self, tasks: dict[str, asyncio.Task[list[Any]]]) -> bool:
        for pid in tasks:
            process = self.store.get_process(pid)
            if process is not None and process.status == ProcessStatus.RUNNING:
                return True
        return False

    def _pid_for_task(self, tasks: dict[str, asyncio.Task[list[Any]]], task: asyncio.Task[list[Any]]) -> str | None:
        for pid, candidate in tasks.items():
            if candidate is task:
                return pid
        return None


class SimpleScheduler(AsyncProcessScheduler):
    pass


def _run_sync(awaitable: Awaitable[Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise RuntimeError("Cannot use sync scheduler APIs inside a running event loop. Use async APIs instead.")


def _budget_exhausted(quanta_used: int, max_quanta: int | None) -> bool:
    return max_quanta is not None and quanta_used >= max_quanta

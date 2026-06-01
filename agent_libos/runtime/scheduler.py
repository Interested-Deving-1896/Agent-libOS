from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.ids import utc_now
from agent_libos.models import ProcessStatus
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.storage import SQLiteStore


Quantum = Callable[[str], Any | Awaitable[Any]]
_SCHEDULER_DEFAULTS = DEFAULT_CONFIG.scheduler


class AsyncProcessScheduler:
    """Cooperative async scheduler for AgentProcess quanta."""

    TERMINAL_STATUSES = {ProcessStatus.EXITED, ProcessStatus.FAILED, ProcessStatus.KILLED}

    def __init__(self, store: SQLiteStore, audit: AuditManager, poll_interval_s: float = _SCHEDULER_DEFAULTS.poll_interval_s):
        self.store = store
        self.audit = audit
        self.poll_interval_s = poll_interval_s

    def next_runnable(self) -> str | None:
        runnable = [p for p in self.store.list_processes() if p.status == ProcessStatus.RUNNABLE]
        runnable.sort(key=lambda proc: proc.created_at)
        return runnable[0].pid if runnable else None

    def runnable_pids(self) -> list[str]:
        runnable = [p for p in self.store.list_processes() if p.status == ProcessStatus.RUNNABLE]
        runnable.sort(key=lambda proc: proc.created_at)
        return [proc.pid for proc in runnable]

    async def arun_once(self, quantum: Quantum) -> Any:
        pid = self.next_runnable()
        if pid is None:
            return None
        return await self._run_quantum(pid, quantum)

    def run_once(self, quantum: Quantum) -> Any:
        return _run_sync(self.arun_once(quantum))

    async def arun_until_idle(self, quantum: Quantum, max_quanta: int = _SCHEDULER_DEFAULTS.max_quanta) -> list[Any]:
        results: list[Any] = []
        tasks: dict[str, asyncio.Task[list[Any]]] = {}
        quanta_used = 0
        quanta_lock = asyncio.Lock()

        async def reserve_quantum() -> bool:
            # The quantum budget is global across process tasks, not per process.
            nonlocal quanta_used
            async with quanta_lock:
                if quanta_used >= max_quanta:
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
                if quanta_used >= max_quanta:
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
                results.extend(task.result())

            if quanta_used >= max_quanta and not done:
                done_all, _pending = await asyncio.wait(tasks.values(), return_when=asyncio.ALL_COMPLETED)
                for task in done_all:
                    pid = self._pid_for_task(tasks, task)
                    if pid is not None:
                        tasks.pop(pid, None)
                    results.extend(task.result())
                break

        return results

    def run_until_idle(self, quantum: Quantum, max_quanta: int = _SCHEDULER_DEFAULTS.max_quanta) -> list[Any]:
        return _run_sync(self.arun_until_idle(quantum, max_quanta=max_quanta))

    async def _run_quantum(self, pid: str, quantum: Quantum) -> Any:
        process = self.store.get_process(pid)
        if process is None or process.status != ProcessStatus.RUNNABLE:
            return None
        process.status = ProcessStatus.RUNNING
        process.updated_at = utc_now()
        self.store.update_process(process)
        self.audit.record(actor="scheduler", action="scheduler.run_quantum", target=f"process:{pid}")
        try:
            result = quantum(pid)
            if inspect.isawaitable(result):
                return await result
            return result
        finally:
            latest = self.store.get_process(pid)
            # A primitive may deliberately set WAITING_HUMAN, EXITED, or another
            # status during the quantum. Only restore RUNNABLE for plain returns.
            if latest is not None and latest.status == ProcessStatus.RUNNING:
                latest.status = ProcessStatus.RUNNABLE
                latest.updated_at = utc_now()
                self.store.update_process(latest)

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

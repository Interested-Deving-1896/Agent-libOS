from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_libos.ids import utc_now
from agent_libos.models import ProcessStatus
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.storage import SQLiteStore


class SimpleScheduler:
    def __init__(self, store: SQLiteStore, audit: AuditManager):
        self.store = store
        self.audit = audit

    def next_runnable(self) -> str | None:
        runnable = [p for p in self.store.list_processes() if p.status == ProcessStatus.RUNNABLE]
        runnable.sort(key=lambda proc: proc.created_at)
        return runnable[0].pid if runnable else None

    def run_once(self, quantum: Callable[[str], Any]) -> Any:
        pid = self.next_runnable()
        if pid is None:
            return None
        process = self.store.get_process(pid)
        if process is None:
            return None
        process.status = ProcessStatus.RUNNING
        process.updated_at = utc_now()
        self.store.update_process(process)
        self.audit.record(actor="scheduler", action="scheduler.run_quantum", target=f"process:{pid}")
        try:
            return quantum(pid)
        finally:
            latest = self.store.get_process(pid)
            if latest is not None and latest.status == ProcessStatus.RUNNING:
                latest.status = ProcessStatus.RUNNABLE
                latest.updated_at = utc_now()
                self.store.update_process(latest)


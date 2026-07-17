from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import replace
from typing import Any

from agent_libos.models import (
    EventPriority,
    EventType,
    ObjectOwnerKind,
    ObjectTask,
    ObjectTaskStatus,
    ProcessStatus,
)
from agent_libos.models.exceptions import NotFound
from agent_libos.ports import AuditPort, EventPort
from agent_libos.runtime.object_task_notifications import (
    ObjectTaskNotificationService,
)
from agent_libos.storage import ObjectRepository, ProcessRepository
from agent_libos.tools.observability import sanitize_for_observability
from agent_libos.utils.ids import utc_now


OBJECT_TASK_ACTIVE_STATUSES = {
    ObjectTaskStatus.QUEUED,
    ObjectTaskStatus.RUNNING,
    ObjectTaskStatus.WAITING_HUMAN,
    ObjectTaskStatus.WAITING_PROCESS,
    ObjectTaskStatus.WAITING_MESSAGE,
}
OBJECT_TASK_TERMINAL_STATUSES = {
    ObjectTaskStatus.SUCCEEDED,
    ObjectTaskStatus.FAILED,
    ObjectTaskStatus.CANCELLED,
    ObjectTaskStatus.ABANDONED,
    ObjectTaskStatus.SUPERSEDED_BY_RESTORE,
    ObjectTaskStatus.RESULT_UNAVAILABLE_AFTER_REOPEN,
}


class ObjectTaskStateService:
    """Own ObjectTask state transitions and runner/result terminal cleanup."""

    def __init__(
        self,
        records: ProcessRepository,
        objects: ObjectRepository,
        process: Any,
        memory: Any,
        audit: AuditPort,
        events: EventPort,
        notifications: ObjectTaskNotificationService,
        lock: AbstractContextManager[Any],
    ) -> None:
        self._records = records
        self._objects = objects
        self._process = process
        self._memory = memory
        self._audit = audit
        self._events = events
        self._notifications = notifications
        self._lock = lock

    def mark_running(self, task: ObjectTask) -> ObjectTask:
        now = utc_now()
        with self._lock:
            latest = self._records.get_object_task(task.task_id) or task
            if latest.status in OBJECT_TASK_TERMINAL_STATUSES:
                return latest
            updated = replace(
                latest,
                status=ObjectTaskStatus.RUNNING,
                started_at=latest.started_at or now,
                updated_at=now,
            )
            self._records.update_object_task(updated)
            if updated.runner_pid is not None:
                # The task and runner transition share the scheduler lock so a
                # winning cancellation cannot be overwritten by stale RUNNING.
                self.set_runner_status(
                    str(updated.runner_pid),
                    ProcessStatus.RUNNING,
                    "object task running",
                )
        self._events.emit(
            EventType.OBJECT_TASK_RUNNING,
            source=updated.creator_pid,
            target=updated.owner_oid,
            payload={
                "task_id": updated.task_id,
                "runner_pid": updated.runner_pid,
                "tool": updated.tool,
            },
        )
        self._audit.record(
            actor="object_task",
            action="object_task.running",
            target=f"object_task:{updated.task_id}",
            decision={"runner_pid": updated.runner_pid, "tool": updated.tool},
        )
        return updated

    def mark_succeeded(
        self,
        task_id: str,
        result: Any,
        result_oid: str | None,
    ) -> ObjectTask:
        now = utc_now()
        with self._lock, self._records.transaction():
            task = self._require(task_id)
            if task.status in OBJECT_TASK_TERMINAL_STATUSES:
                return task
            updated = replace(
                task,
                status=ObjectTaskStatus.SUCCEEDED,
                result_oid=result_oid,
                error=None,
                updated_at=now,
                completed_at=now,
            )
            self._records.update_object_task(updated)
            self._events.emit(
                EventType.OBJECT_TASK_COMPLETED,
                source=updated.creator_pid,
                target=updated.owner_oid,
                payload={
                    "task_id": task_id,
                    "result_oid": result_oid,
                    "tool": updated.tool,
                },
            )
            self._audit.record(
                actor="object_task",
                action="object_task.completed",
                target=f"object_task:{task_id}",
                output_refs=[result_oid] if result_oid else [],
                decision={
                    "ok": True,
                    "tool": updated.tool,
                    "call_id": getattr(result, "call_id", None),
                },
            )
        notified = self._notifications.notify_terminal(updated, phase="completed")
        self.cleanup_owner_pin_after_terminal(notified)
        return notified

    def mark_failed(self, task_id: str, error: str) -> ObjectTask:
        now = utc_now()
        with self._lock, self._records.transaction():
            task = self._require(task_id)
            if task.status in OBJECT_TASK_TERMINAL_STATUSES:
                return task
            updated = replace(
                task,
                status=ObjectTaskStatus.FAILED,
                error=error,
                updated_at=now,
                completed_at=now,
            )
            self._records.update_object_task(updated)
            self._events.emit(
                EventType.OBJECT_TASK_FAILED,
                source="object_task",
                target=updated.owner_oid,
                payload={
                    "task_id": task_id,
                    "tool": updated.tool,
                    "error": error,
                },
                priority=EventPriority.HIGH,
            )
            self._audit.record(
                actor="object_task",
                action="object_task.failed",
                target=f"object_task:{task_id}",
                decision={
                    "tool": updated.tool,
                    "error": sanitize_for_observability(error),
                },
            )
        notified = self._notifications.notify_terminal(updated, phase="failed")
        self.cleanup_owner_pin_after_terminal(notified)
        return notified

    def mark_waiting(
        self,
        task_id: str,
        status: ObjectTaskStatus,
        wait: dict[str, Any],
        message: str,
    ) -> ObjectTask:
        now = utc_now()
        with self._lock:
            task = self._require(task_id)
            if task.status in OBJECT_TASK_TERMINAL_STATUSES:
                return task
            updated = replace(
                task,
                status=status,
                wait=wait,
                error=message,
                updated_at=now,
            )
            self._records.update_object_task(updated)
        self._events.emit(
            EventType.OBJECT_TASK_WAITING,
            source="object_task",
            target=updated.owner_oid,
            payload={
                "task_id": task_id,
                "status": status.value,
                "wait": wait,
                "tool": updated.tool,
            },
            priority=EventPriority.HIGH,
        )
        self._audit.record(
            actor="object_task",
            action="object_task.waiting",
            target=f"object_task:{task_id}",
            decision={"status": status.value, "wait": wait, "tool": updated.tool},
        )
        return self._notifications.notify(updated, phase="waiting")

    def mark_cancelled(
        self,
        task: ObjectTask,
        *,
        actor: str,
        reason: str,
    ) -> ObjectTask:
        now = utc_now()
        with self._lock, self._records.transaction():
            latest = self._records.get_object_task(task.task_id) or task
            if latest.status in OBJECT_TASK_TERMINAL_STATUSES:
                return latest
            updated = replace(
                latest,
                status=ObjectTaskStatus.CANCELLED,
                error=reason,
                updated_at=now,
                completed_at=now,
            )
            self._records.update_object_task(updated)
            if updated.runner_pid is not None:
                self.terminalize_runner(str(updated.runner_pid), reason=reason)
            self._events.emit(
                EventType.OBJECT_TASK_CANCELLED,
                source=actor,
                target=updated.owner_oid,
                payload={"task_id": updated.task_id, "reason": reason},
            )
            self._audit.record(
                actor=actor,
                action="object_task.cancel",
                target=f"object_task:{updated.task_id}",
                decision={"reason": reason},
            )
        notified = self._notifications.notify_terminal(updated, phase="cancelled")
        self.cleanup_owner_pin_after_terminal(notified)
        return notified

    def set_runner_status(
        self,
        runner_pid: str,
        status: ProcessStatus,
        message: str | None = None,
    ) -> None:
        with self._records.locked():
            process = self._records.get_process(runner_pid)
            if (
                process is None
                or process.status in self._process.TERMINAL_STATUSES
            ):
                return
            process.status = status
            process.status_message = message
            process.updated_at = utc_now()
            self._records.transition_process(
                runner_pid,
                status,
                expected_revision=process.revision,
                status_message=message,
            )

    def terminalize_runner(self, runner_pid: str, *, reason: str) -> None:
        process = self._records.get_process(runner_pid)
        if process is None or process.status in self._process.TERMINAL_STATUSES:
            return
        try:
            self._process.signal(runner_pid, "cancel", {"reason": reason})
        except Exception:
            process = self._records.get_process(runner_pid)
            if (
                process is not None
                and process.status not in self._process.TERMINAL_STATUSES
            ):
                process.status = ProcessStatus.KILLED
                process.status_message = reason
                process.updated_at = utc_now()
                self._records.transition_process(
                    runner_pid,
                    ProcessStatus.KILLED,
                    expected_revision=process.revision,
                    status_message=reason,
                )

    def discard_failed_result(
        self,
        runner_pid: str,
        task_id: str,
        result_oid: str | None,
    ) -> None:
        if result_oid is None:
            return
        creator = self._records.get_object_task(task_id)
        if creator is not None:
            process = self._records.get_process(creator.creator_pid)
            if process is not None and process.memory_view is not None:
                original_count = len(process.memory_view.roots)
                process.memory_view.roots = [
                    handle
                    for handle in process.memory_view.roots
                    if handle.oid != result_oid
                ]
                if len(process.memory_view.roots) != original_count:
                    self._records.remove_process_memory_roots(
                        process.pid,
                        [result_oid],
                    )
        obj = self._objects.get_object(result_oid)
        if obj is None:
            return
        owned_by_runner = (
            obj.owner_kind == ObjectOwnerKind.PROCESS
            and obj.owner_id == runner_pid
        )
        owned_by_task = (
            obj.owner_kind == ObjectOwnerKind.OBJECT_TASK
            and obj.owner_id == task_id
        )
        if not owned_by_runner and not owned_by_task:
            return
        self._memory.delete_object_trusted(
            "object_task",
            result_oid,
            reason="failed_or_cancelled_object_task_result",
        )
        self._audit.record(
            actor="object_task",
            action="object_task.discard_uncommitted_result",
            target=f"object:{result_oid}",
            input_refs=[result_oid],
            decision={"runner_pid": runner_pid, "task_id": task_id},
        )

    def cleanup_owner_pin_after_terminal(self, task: ObjectTask) -> None:
        creator = self._records.get_process(task.creator_pid)
        if (
            creator is None
            or creator.status not in self._process.TERMINAL_STATUSES
        ):
            return
        self._memory.release_owner(
            ObjectOwnerKind.PROCESS,
            task.creator_pid,
            actor="object_task",
            reason="creator_process_owned_release_after_object_task_terminal",
        )

    def _require(self, task_id: str) -> ObjectTask:
        task = self._records.get_object_task(task_id)
        if task is None:
            raise NotFound(f"object task not found: {task_id}")
        return task

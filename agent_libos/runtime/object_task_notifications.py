from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import replace
from typing import Any, AbstractSet

from agent_libos.models import (
    EventType,
    ObjectTask,
    ObjectTaskNotificationStatus,
    ObjectTaskStatus,
    ProcessStatus,
)
from agent_libos.models.exceptions import CapabilityDenied, ProcessError
from agent_libos.ports import EventPort
from agent_libos.runtime.message_manager import ProcessMessageManager
from agent_libos.storage import ProcessRepository
from agent_libos.utils.ids import utc_now


_TERMINAL_NOTIFICATION_PHASES = {
    ObjectTaskStatus.SUCCEEDED: "completed",
    ObjectTaskStatus.FAILED: "failed",
    ObjectTaskStatus.CANCELLED: "cancelled",
}


class ObjectTaskNotificationService:
    """Durable ObjectTask message delivery and terminal retry policy."""

    def __init__(
        self,
        records: ProcessRepository,
        messages: ProcessMessageManager,
        events: EventPort,
        terminal_process_statuses: AbstractSet[ProcessStatus],
        lock: AbstractContextManager[Any],
    ) -> None:
        self._records = records
        self._messages = messages
        self._events = events
        self._terminal_process_statuses = terminal_process_statuses
        self._lock = lock

    def notify(self, task: ObjectTask, *, phase: str) -> ObjectTask:
        with self._records.transaction():
            return self._notify_in_transaction(task, phase=phase)

    def notify_terminal(self, task: ObjectTask, *, phase: str) -> ObjectTask:
        with self._lock:
            return self._notify_terminal_locked(task, phase=phase)

    def retry_terminal(self, task: ObjectTask) -> ObjectTask:
        phase = _TERMINAL_NOTIFICATION_PHASES.get(task.status)
        if phase is None or task.notification.recipient_pid is None:
            return task
        if task.notification.status not in {
            ObjectTaskNotificationStatus.NONE,
            ObjectTaskNotificationStatus.FAILED,
        }:
            return task
        return self.notify_terminal(task, phase=phase)

    def _notify_in_transaction(self, task: ObjectTask, *, phase: str) -> ObjectTask:
        notification = task.notification
        if notification.recipient_pid is None:
            return task
        payload = {
            "type": "object_task",
            "phase": phase,
            "task_id": task.task_id,
            "owner_oid": task.owner_oid,
            "tool": task.tool,
            "status": task.status.value,
            "result_oid": task.result_oid,
            "error": task.error,
            "wait": task.wait,
        }
        source_oids = [task.owner_oid]
        if task.result_oid is not None:
            source_oids.append(task.result_oid)
        try:
            message = self._messages.post(
                sender=f"object_task:{task.task_id}",
                recipient_pid=notification.recipient_pid,
                kind=notification.kind,
                channel=notification.channel,
                correlation_id=task.task_id,
                subject=(
                    notification.subject
                    or f"Object task {task.status.value}: {task.tool}"
                ),
                body=task.error or "",
                payload=payload,
                source_oids=source_oids,
            )
            updated_notification = replace(
                notification,
                message_id=message.message_id,
                status=ObjectTaskNotificationStatus.DELIVERED,
                error=None,
            )
        except (CapabilityDenied, ProcessError) as exc:
            recipient = self._records.get_process(notification.recipient_pid)
            status = (
                ObjectTaskNotificationStatus.UNDELIVERED_TERMINAL
                if recipient is not None
                and recipient.status in self._terminal_process_statuses
                else ObjectTaskNotificationStatus.FAILED
            )
            updated_notification = replace(
                notification,
                status=status,
                error=str(exc),
            )
            if status == ObjectTaskNotificationStatus.UNDELIVERED_TERMINAL:
                self._events.emit(
                    EventType.OBJECT_TASK_NOTIFICATION_UNDELIVERED,
                    source="object_task",
                    target=notification.recipient_pid,
                    payload={
                        "task_id": task.task_id,
                        "status": task.status.value,
                        "reason": "terminal_process",
                    },
                )
        updated = replace(
            task,
            notification=updated_notification,
            updated_at=utc_now(),
        )
        self._records.update_object_task(updated)
        return updated

    def _notify_terminal_locked(self, task: ObjectTask, *, phase: str) -> ObjectTask:
        """Settle post-commit delivery without hiding a terminal transition."""

        try:
            return self.notify(task, phase=phase)
        except Exception as exc:
            with self._records.transaction():
                latest = self._records.get_object_task(task.task_id) or task
                if (
                    latest.notification.status
                    == ObjectTaskNotificationStatus.DELIVERED
                ):
                    return latest
                notification = replace(
                    latest.notification,
                    status=ObjectTaskNotificationStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                )
                updated = replace(
                    latest,
                    notification=notification,
                    updated_at=utc_now(),
                )
                self._records.update_object_task(updated)
                return updated

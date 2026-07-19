from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.models.base import OID, PID, StrEnum, ToolID


class ObjectTaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    WAITING_PROCESS = "waiting_process"
    WAITING_MESSAGE = "waiting_message"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    SUPERSEDED_BY_RESTORE = "superseded_by_restore"
    RESULT_UNAVAILABLE_AFTER_REOPEN = "result_unavailable_after_reopen"


class ObjectTaskNotificationStatus(StrEnum):
    NONE = "none"
    DELIVERED = "delivered"
    UNDELIVERED_TERMINAL = "undelivered_terminal"
    FAILED = "failed"


class ObjectTaskRecoveryKind(StrEnum):
    ACTIVE = "active"
    MISSING_RESULT = "missing_result"
    NOTIFICATION = "notification"


@dataclass(frozen=True, order=True, slots=True)
class ObjectTaskRecoveryCursor:
    created_at: str
    task_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.created_at, str) or not self.created_at:
            raise ValueError("object task recovery cursor created_at must not be empty")
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("object task recovery cursor task_id must not be empty")


@dataclass
class ObjectTaskOwnerWatch:
    enabled: bool = False
    events: list[str] = field(default_factory=lambda: ["updated", "linked"])
    kind: str = "normal"
    channel: str = "object-task-owner"


@dataclass
class ObjectTaskNotification:
    recipient_pid: PID | None = None
    kind: str = "normal"
    channel: str = "object-task"
    subject: str | None = None
    message_id: str | None = None
    status: ObjectTaskNotificationStatus = ObjectTaskNotificationStatus.NONE
    error: str | None = None


@dataclass
class ObjectTask:
    task_id: str
    owner_oid: OID
    creator_pid: PID
    runner_pid: PID | None
    tool: str
    tool_id: ToolID | None
    status: ObjectTaskStatus
    notification: ObjectTaskNotification = field(default_factory=ObjectTaskNotification)
    owner_watch: ObjectTaskOwnerWatch = field(default_factory=ObjectTaskOwnerWatch)
    result_oid: OID | None = None
    error: str | None = None
    wait: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True, slots=True)
class ObjectTaskRecoveryPage:
    records: tuple[ObjectTask, ...]
    next_cursor: ObjectTaskRecoveryCursor | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple):
            raise ValueError("object task recovery records must be a tuple")
        if any(not isinstance(record, ObjectTask) for record in self.records):
            raise ValueError("object task recovery page contains an invalid record")
        keys = [(record.created_at, record.task_id) for record in self.records]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("object task recovery page records must be strictly ordered")
        if self.next_cursor is not None:
            if not self.records:
                raise ValueError("empty object task recovery page cannot have a cursor")
            if self.next_cursor != ObjectTaskRecoveryCursor(*keys[-1]):
                raise ValueError("object task recovery cursor must match the last record")


@dataclass(frozen=True, slots=True)
class ObjectTaskRecoverySummary:
    """Exact recovery totals with bounded representative task identifiers."""

    abandoned_total: int = 0
    result_unavailable_total: int = 0
    notification_retried_total: int = 0
    abandoned_sample: tuple[str, ...] = ()
    result_unavailable_sample: tuple[str, ...] = ()
    notification_retried_sample: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "abandoned_total",
            "result_unavailable_total",
            "notification_retried_total",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"object task recovery {name} must be non-negative")
        for name, total in (
            ("abandoned_sample", self.abandoned_total),
            ("result_unavailable_sample", self.result_unavailable_total),
            ("notification_retried_sample", self.notification_retried_total),
        ):
            sample = getattr(self, name)
            if not isinstance(sample, tuple):
                raise ValueError(f"object task recovery {name} must be a tuple")
            if len(sample) > total:
                raise ValueError(f"object task recovery {name} exceeds total")
            if any(not isinstance(task_id, str) or not task_id for task_id in sample):
                raise ValueError(f"object task recovery {name} IDs must not be empty")

    @property
    def total_count(self) -> int:
        return (
            self.abandoned_total
            + self.result_unavailable_total
            + self.notification_retried_total
        )

    @property
    def truncated(self) -> bool:
        return any(
            total > len(sample)
            for total, sample in (
                (self.abandoned_total, self.abandoned_sample),
                (self.result_unavailable_total, self.result_unavailable_sample),
                (self.notification_retried_total, self.notification_retried_sample),
            )
        )

    def __len__(self) -> int:
        return self.total_count

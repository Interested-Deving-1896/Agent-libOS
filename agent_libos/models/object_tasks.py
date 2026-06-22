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


class ObjectTaskNotificationStatus(StrEnum):
    NONE = "none"
    DELIVERED = "delivered"
    UNDELIVERED_TERMINAL = "undelivered_terminal"
    FAILED = "failed"


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

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.models.base import PID, ProcessMessageID, StrEnum


class ProcessMessageKind(StrEnum):
    NORMAL = "normal"
    INTERRUPT = "interrupt"


class ProcessMessageStatus(StrEnum):
    UNREAD = "unread"
    ACKED = "acked"


@dataclass
class ProcessMessage:
    message_id: ProcessMessageID
    sender: str
    recipient_pid: PID
    kind: ProcessMessageKind
    subject: str
    body: str
    channel: str = "default"
    correlation_id: str | None = None
    reply_to: ProcessMessageID | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    status: ProcessMessageStatus = ProcessMessageStatus.UNREAD
    created_at: str = ""
    updated_at: str = ""
    acked_at: str | None = None

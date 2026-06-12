from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.models.base import EventID, StrEnum


class EventType(StrEnum):
    PROCESS_CREATED = "process_created"
    PROCESS_FORKED = "process_forked"
    PROCESS_EXEC = "process_exec"
    PROCESS_EXITED = "process_exited"
    PROCESS_MESSAGE_POSTED = "process_message_posted"
    PROCESS_MESSAGE_NOTICE = "process_message_notice"
    PROCESS_MESSAGE_ACKED = "process_message_acked"
    PROCESS_SIGNAL = "process_signal"
    OBJECT_CREATED = "object_created"
    OBJECT_UPDATED = "object_updated"
    OBJECT_LINKED = "object_linked"
    HUMAN_QUERY = "human_query"
    HUMAN_RESPONSE = "human_response"
    IMAGE_REGISTERED = "image_registered"
    SKILL_REGISTERED = "skill_registered"
    SKILL_LOADED = "skill_loaded"
    SKILL_UNLOADED = "skill_unloaded"
    SKILL_TRUSTED = "skill_trusted"
    MODULE_LOADED = "module_loaded"
    TOOL_CALLED = "tool_called"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    CAPABILITY_GRANTED = "capability_granted"
    CAPABILITY_REVOKED = "capability_revoked"
    CHECKPOINT_CREATED = "checkpoint_created"
    ROLLBACK = "rollback"
    EXTERNAL_READ = "external_read"
    EXTERNAL_WRITE = "external_write"
    HUMAN_OUTPUT = "human_output"


class EventPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Event:
    event_id: EventID
    type: EventType
    source: str
    target: str | None
    payload: dict[str, Any]
    priority: EventPriority
    created_at: str
    correlation_id: str | None = None
    causality: dict[str, Any] = field(default_factory=dict)

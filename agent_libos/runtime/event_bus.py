from __future__ import annotations

import builtins
from typing import Any

from agent_libos.utils.ids import new_id, utc_now
from agent_libos.models import Event, EventPriority, EventType
from agent_libos.storage import SQLiteStore


class EventBus:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def emit(
        self,
        event_type: EventType | str,
        source: str,
        target: str | None = None,
        payload: dict[str, Any] | None = None,
        priority: EventPriority | str = EventPriority.NORMAL,
        correlation_id: str | None = None,
        causality: dict[str, Any] | None = None,
    ) -> Event:
        event = Event(
            event_id=new_id("evt"),
            type=EventType(event_type),
            source=source,
            target=target,
            payload=payload or {},
            priority=EventPriority(priority),
            created_at=utc_now(),
            correlation_id=correlation_id,
            causality=causality or {},
        )
        self.store.insert_event(event)
        return event

    def list(self, target: str | None = None) -> builtins.list[Event]:
        return self.store.list_events(target=target)

from __future__ import annotations

import builtins
from typing import Any

from agent_libos.utils.ids import new_id, utc_now
from agent_libos.models import Event, EventPriority, EventType
from agent_libos.models.exceptions import ValidationError
from agent_libos.storage import RuntimeStore


class EventBus:
    def __init__(self, store: RuntimeStore):
        self.store = store
        self.operations: Any | None = None

    def bind_operations(self, operations: Any) -> None:
        self.operations = operations

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
        if self.operations is not None:
            self.operations.link_evidence("event", event.event_id, "event")
            if event.type == EventType.RESOURCE_CHARGED:
                self.operations.link_evidence("event", event.event_id, "resource_charge")
        return event

    def list(
        self,
        target: str | None = None,
        limit: int | None = None,
        before_event_id: str | None = None,
        after_event_id: str | None = None,
        *,
        include_gui_presentation: bool = True,
    ) -> builtins.list[Event]:
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
            raise ValidationError("event list limit must be a positive integer")
        if before_event_id is not None and after_event_id is not None:
            raise ValidationError("event query cannot use before_event_id and after_event_id together")
        filters = {
            "target": target,
            "limit": limit,
            "before_event_id": before_event_id,
            "after_event_id": after_event_id,
        }
        if not include_gui_presentation:
            filters["include_gui_presentation"] = False
        return self.store.list_events(**filters)

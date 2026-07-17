from __future__ import annotations

from typing import Any, Protocol

from agent_libos.models import Event, EventPriority, EventType


class EventPort(Protocol):
    """Minimal durable event sink consumed by core services."""

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
        ...

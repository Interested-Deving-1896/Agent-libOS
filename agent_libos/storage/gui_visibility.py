from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_libos.models import EventType


def is_gui_presentation_event(event: Any) -> bool:
    return is_gui_presentation_event_fields(
        getattr(event, "type", None),
        getattr(event, "payload", None),
    )


def is_gui_presentation_event_fields(
    event_type: EventType | str | None,
    payload: Any,
) -> bool:
    try:
        selected_type = EventType(event_type)
    except (TypeError, ValueError):
        return False
    selected_payload = payload if isinstance(payload, Mapping) else {}
    if selected_type == EventType.HUMAN_OUTPUT:
        return selected_payload.get("purpose") == "gui_presentation"
    return bool(
        selected_type == EventType.DATA_FLOW_DECISION
        and _is_gui_human_sink(selected_payload.get("sink"))
    )


def is_gui_presentation_audit(record: Any) -> bool:
    return is_gui_presentation_audit_fields(
        getattr(record, "action", None),
        getattr(record, "target", None),
        getattr(record, "decision", None),
    )


def is_gui_presentation_audit_fields(
    action: str | None,
    target: str | None,
    decision: Any,
) -> bool:
    selected_decision = decision if isinstance(decision, Mapping) else {}
    if selected_decision.get("purpose") == "gui_presentation":
        return True
    return action == "data_flow.egress" and _is_gui_human_sink(target)


def _is_gui_human_sink(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("human:")
        and value.endswith(":gui")
    )

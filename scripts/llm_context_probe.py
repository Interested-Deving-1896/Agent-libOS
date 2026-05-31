from __future__ import annotations

import json
import re
from typing import Any


def static_prefix(messages: list[dict[str, Any]]) -> dict[str, Any]:
    text = _message_text(messages)
    match = re.search(r"Static prefix:\n(?P<payload>.*?)\n\nAppend-only entries:", text, flags=re.DOTALL)
    if match is None:
        return {}
    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def entries(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = _message_text(messages)
    if "Append-only entries:" not in text:
        return []
    tail = text.split("Append-only entries:", 1)[1]
    result: list[dict[str, Any]] = []
    for block in re.split(r"(?m)^---\s*$", tail):
        block = block.strip()
        if not block:
            continue
        try:
            entry = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            result.append(entry)
    return result


def recent_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entry in entries(messages):
        if entry.get("kind") != "events_delta":
            continue
        events = entry.get("events")
        if isinstance(events, list):
            result.extend(event for event in events if isinstance(event, dict))
    return result


def tool_result_payloads(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entry in entries(messages):
        if entry.get("kind") != "memory_delta":
            continue
        objects = entry.get("objects")
        if not isinstance(objects, list):
            continue
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            payload = obj.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("tool_name"), str):
                result.append(payload)
    return result


def last_tool_result(messages: list[dict[str, Any]], tool_name: str) -> dict[str, Any] | None:
    for payload in reversed(tool_result_payloads(messages)):
        if payload.get("tool_name") != tool_name:
            continue
        result = payload.get("result")
        if isinstance(result, dict):
            return result
    return None


def _message_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(message.get("content", "")) for message in messages)

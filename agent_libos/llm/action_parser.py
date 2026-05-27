from __future__ import annotations

import json
from typing import Any


def parse_json_action(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _strip_fence(stripped)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        value = json.loads(_extract_last_action_object(stripped))
    if not isinstance(value, dict):
        raise ValueError("LLM action must be a JSON object")
    if "action" not in value:
        raise ValueError("LLM action missing 'action'")
    return value


def _strip_fence(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_last_action_object(text: str) -> str:
    candidates = _extract_json_objects(text)
    for candidate in reversed(candidates):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "action" in value:
            return candidate
    raise ValueError("no JSON action object found")


def _extract_json_objects(text: str) -> list[str]:
    objects: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start : index + 1])
                start = None
    return objects

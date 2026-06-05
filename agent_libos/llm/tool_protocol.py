from __future__ import annotations

import json
from typing import Any


def tool_call_to_action(tool_call: dict[str, Any]) -> dict[str, Any]:
    name = str(tool_call.get("name") or "").strip()
    raw_args = tool_call.get("arguments")
    # Empty arguments are common for no-arg tool calls, but other false-y
    # values such as [] or 0 are malformed protocol frames and should surface
    # as repairable LLM output errors.
    if raw_args is None or raw_args == "":
        raw_args = "{}"
    if isinstance(raw_args, str):
        args = json.loads(raw_args or "{}")
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        raise ValueError(f"invalid tool arguments for {name}: {type(raw_args).__name__}")
    if not isinstance(args, dict):
        raise ValueError(f"tool arguments for {name} must decode to an object")
    if not name:
        fallback_name = str(args.get("action") or "").strip()
        if not fallback_name:
            raise ValueError("tool call is missing a function name")
        name = fallback_name
    args = {key: value for key, value in args.items() if key != "action"}
    return {**args, "action": name}

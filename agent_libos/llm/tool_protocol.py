from __future__ import annotations

import json
from typing import Any


def tool_call_to_action(tool_call: dict[str, Any]) -> dict[str, Any]:
    name = tool_call["name"]
    raw_args = tool_call.get("arguments") or "{}"
    if isinstance(raw_args, str):
        args = json.loads(raw_args or "{}")
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        raise ValueError(f"invalid tool arguments for {name}: {type(raw_args).__name__}")
    if not isinstance(args, dict):
        raise ValueError(f"tool arguments for {name} must decode to an object")
    return {"action": name, **args}

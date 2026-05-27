from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, set):
        return sorted(to_jsonable(item) for item in value)
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value


def dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=True, sort_keys=True)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def estimate_tokens(value: object) -> int:
    text = value if isinstance(value, str) else repr(value)
    return max(1, len(text) // 4)


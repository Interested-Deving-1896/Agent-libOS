from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentRating:
    rating_id: str
    pid: str
    score: int
    comment: str
    rater: str
    source: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

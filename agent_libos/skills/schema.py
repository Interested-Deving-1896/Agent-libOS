from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SkillSpec:
    skill_id: str
    name: str
    version: str = "v0"
    description: str = ""
    required_capabilities: list[dict[str, Any]] = field(default_factory=list)
    signed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


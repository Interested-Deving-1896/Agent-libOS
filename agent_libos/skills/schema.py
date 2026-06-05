from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActionSchema:
    name: str
    use_cases: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    required_capabilities: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    failure_modes: list[str] = field(default_factory=list)
    examples: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class JitToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    tests: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillSpec:
    skill_id: str
    name: str
    version: str = "v0"
    description: str = ""
    instructions: str = ""
    tools: list[str] = field(default_factory=list)
    actions: list[ActionSchema] = field(default_factory=list)
    jit_tools: list[JitToolSpec] = field(default_factory=list)
    required_capabilities: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    signature: str | None = None
    schema_version: int = 1

    @property
    def signed(self) -> bool:
        return bool(self.signature)


@dataclass(frozen=True)
class LoadedSkill:
    skill_id: str
    version: str
    source: str | None
    loaded_at: str
    tool_names: list[str]
    tool_ids: dict[str, str]
    jit_tool_ids: dict[str, str]
    instructions_hash: str

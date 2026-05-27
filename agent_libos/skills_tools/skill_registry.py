from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.exceptions import NotFound
from agent_libos.skills_tools.action_schema import ActionSchema


@dataclass(frozen=True)
class SkillDescriptor:
    skill_id: str
    name: str
    version: str
    description: str = ""
    actions: list[ActionSchema] = field(default_factory=list)
    required_capabilities: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    signed: bool = False


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, SkillDescriptor] = {}

    def register(self, skill: SkillDescriptor) -> SkillDescriptor:
        self._skills[skill.skill_id] = skill
        return skill

    def get(self, skill_id: str) -> SkillDescriptor:
        try:
            return self._skills[skill_id]
        except KeyError as exc:
            raise NotFound(f"skill not found: {skill_id}") from exc

    def discover(self, text: str | None = None) -> list[SkillDescriptor]:
        values = list(self._skills.values())
        if text is None:
            return values
        needle = text.lower()
        return [
            skill
            for skill in values
            if needle in skill.name.lower() or needle in skill.description.lower() or needle in skill.skill_id.lower()
        ]


from __future__ import annotations

from agent_libos.exceptions import NotFound
from agent_libos.skills.schema import SkillSpec


class RuntimeSkillRegistry:
    def __init__(self):
        self._skills: dict[str, SkillSpec] = {}

    def register(self, skill: SkillSpec) -> SkillSpec:
        self._skills[skill.skill_id] = skill
        return skill

    def get(self, skill_id: str) -> SkillSpec:
        try:
            return self._skills[skill_id]
        except KeyError as exc:
            raise NotFound(f"skill not found: {skill_id}") from exc

    def list(self) -> list[SkillSpec]:
        return list(self._skills.values())


from __future__ import annotations

from agent_libos.skills.schema import SkillSpec


class SkillVerifier:
    def verify(self, skill: SkillSpec, require_signature: bool = True) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if not skill.skill_id:
            errors.append("skill_id is required")
        if not skill.name:
            errors.append("name is required")
        if require_signature and not skill.signed:
            errors.append("unsigned skill")
        return not errors, errors


from __future__ import annotations

import json
from pathlib import Path

from agent_libos.skills_tools.action_schema import ActionSchema
from agent_libos.skills_tools.skill_registry import SkillDescriptor


def load_skill_descriptor(path: str | Path) -> SkillDescriptor:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    actions = [ActionSchema(**item) for item in data.get("actions", [])]
    return SkillDescriptor(
        skill_id=data["skill_id"],
        name=data["name"],
        version=data.get("version", "v0"),
        description=data.get("description", ""),
        actions=actions,
        required_capabilities=data.get("required_capabilities", []),
        metadata=data.get("metadata", {}),
        signed=data.get("signed", False),
    )


from __future__ import annotations

from agent_libos.exceptions import ValidationError
from agent_libos.ids import utc_now
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.skills.registry import RuntimeSkillRegistry
from agent_libos.skills.schema import SkillSpec
from agent_libos.skills.verifier import SkillVerifier
from agent_libos.storage import SQLiteStore


class SkillLinker:
    def __init__(
        self,
        store: SQLiteStore,
        registry: RuntimeSkillRegistry,
        audit: AuditManager,
        verifier: SkillVerifier | None = None,
    ):
        self.store = store
        self.registry = registry
        self.audit = audit
        self.verifier = verifier or SkillVerifier()

    def dlopen_skill(self, pid: str, skill: SkillSpec, require_signature: bool = False) -> str:
        ok, errors = self.verifier.verify(skill, require_signature=require_signature)
        if not ok:
            raise ValidationError("; ".join(errors))
        self.registry.register(skill)
        process = self.store.get_process(pid)
        if process is not None:
            process.loaded_skills[skill.skill_id] = skill.version
            process.updated_at = utc_now()
            self.store.update_process(process)
        self.audit.record(
            actor=pid,
            action="skill.load",
            target=f"skill:{skill.skill_id}",
            decision={"version": skill.version, "signed": skill.signed},
        )
        return skill.skill_id

    def unload_skill(self, pid: str, skill_id: str) -> None:
        process = self.store.get_process(pid)
        if process is not None:
            process.loaded_skills.pop(skill_id, None)
            process.updated_at = utc_now()
            self.store.update_process(process)
        self.audit.record(actor=pid, action="skill.unload", target=f"skill:{skill_id}")


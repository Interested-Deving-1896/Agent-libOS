from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models import CapabilityRight, EventType, ToolCandidateStatus
from agent_libos.models.exceptions import CapabilityDenied, HumanApprovalRequired, NotFound, ValidationError
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.skills.schema import ActionSchema, JitToolSpec, LoadedSkill, SkillSpec
from agent_libos.storage import SQLiteStore
from agent_libos.utils.ids import new_id, utc_now
from agent_libos.utils.serde import dumps, to_jsonable
from agent_libos.utils.yaml_loader import load_yaml_mapping

_SKILL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]*$")
_SOURCE_TYPES = {"workspace", "global", "runtime", "inline"}


class SkillManager:
    """Capability-controlled primitive for registering and loading skills.

    Skill loading changes LLM-facing tool visibility and prompt metadata only.
    It never grants external-resource authority; the underlying primitives keep
    enforcing filesystem, shell, human, object, process, and image capabilities.
    """

    def __init__(
        self,
        store: SQLiteStore,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        *,
        config: AgentLibOSConfig | None = None,
        human: Any | None = None,
    ):
        self.config = config or DEFAULT_CONFIG
        self.store = store
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        self.human = human
        self.runtime: Any | None = None

    def bind_runtime(self, runtime: Any) -> None:
        self.runtime = runtime

    def resource_for(self, skill_id: str) -> str:
        return f"skill:{skill_id}"

    def trust_resource(self, manifest_sha256: str = "*") -> str:
        return self.config.skills.trust_resource if manifest_sha256 == "*" else f"skill_trust:{manifest_sha256}"

    def source_resource(self, source_type: str, source: str) -> str:
        return f"skill_source:{source_type}:{source}"

    def register_skill(
        self,
        skill: SkillSpec | dict[str, Any],
        *,
        actor: str,
        replace: bool = False,
        require_capability: bool = True,
        source_type: str = "inline",
        source: str | None = None,
        manifest_sha256: str | None = None,
    ) -> dict[str, Any]:
        spec = self._coerce_skill(skill)
        self._validate_skill(spec)
        selected_source_type = self._validate_source_type(source_type)
        selected_source = source or selected_source_type
        selected_sha = manifest_sha256 or self._hash_manifest(dumps(spec))
        if selected_source_type == "global":
            self._require_trusted_global_source(selected_source, selected_sha)
        if require_capability:
            self._require_skill_right(actor, spec.skill_id, CapabilityRight.WRITE)
        existing = self.store.get_skill(spec.skill_id)
        if existing is not None and not replace:
            raise ValidationError(f"skill already registered: {spec.skill_id}")
        now = utc_now()
        self.store.upsert_skill(
            spec,
            source_type=selected_source_type,
            source=selected_source,
            manifest_sha256=selected_sha,
            registered_by=actor,
            created_at=now,
        )
        self.capabilities.consume_allow_once(actor, self.resource_for(spec.skill_id), CapabilityRight.WRITE, "skill")
        self.events.emit(
            EventType.SKILL_REGISTERED,
            source=actor,
            target=self.resource_for(spec.skill_id),
            payload={"skill_id": spec.skill_id, "version": spec.version, "source_type": selected_source_type},
        )
        self.audit.record(
            actor=actor,
            action="skill.register",
            target=self.resource_for(spec.skill_id),
            decision={
                "replace": existing is not None,
                "source_type": selected_source_type,
                "source": selected_source,
                "manifest_sha256": selected_sha,
                "tools": list(spec.tools),
                "jit_tools": [tool.name for tool in spec.jit_tools],
            },
        )
        return self.inspect_skill(spec.skill_id, actor=actor, require_capability=False)

    def register_skill_from_yaml_text(
        self,
        text: str,
        *,
        actor: str,
        replace: bool = False,
        require_capability: bool = True,
        source_type: str = "workspace",
        source: str | None = None,
    ) -> dict[str, Any]:
        self._check_manifest_size(text)
        source_id = source or source_type
        spec = self.parse_manifest_text(text)
        return self.register_skill(
            spec,
            actor=actor,
            replace=replace,
            require_capability=require_capability,
            source_type=source_type,
            source=source_id,
            manifest_sha256=self._hash_manifest(text),
        )

    def register_global_skill_from_path(
        self,
        path: str | Path,
        *,
        actor: str,
        replace: bool = False,
        require_capability: bool = True,
    ) -> dict[str, Any]:
        absolute, source_id = self._normalize_global_path(path)
        text = self._read_text_limited(absolute)
        return self.register_skill_from_yaml_text(
            text,
            actor=actor,
            replace=replace,
            require_capability=require_capability,
            source_type="global",
            source=source_id,
        )

    def global_manifest_info(self, path: str | Path) -> dict[str, Any]:
        absolute, source_id = self._normalize_global_path(path)
        text = self._read_text_limited(absolute)
        return {
            "path": str(absolute),
            "source": source_id,
            "manifest_sha256": self._hash_manifest(text),
            "bytes": len(text.encode("utf-8")),
        }

    def load_skill_from_workspace_yaml(
        self,
        pid: str,
        path: str,
        *,
        replace: bool = False,
        require_capability: bool = True,
    ) -> dict[str, Any]:
        runtime = self._runtime()
        cwd = runtime.process.working_directory(pid)
        read = runtime.filesystem.read_text(
            pid,
            path,
            max_bytes=self.config.skills.manifest_max_bytes,
            cwd=cwd,
        )
        self._check_manifest_size(read.content)
        spec = self.parse_manifest_text(read.content)
        if require_capability:
            self._require_skill_rights(pid, spec.skill_id, [CapabilityRight.WRITE, CapabilityRight.EXECUTE])
        self.register_skill(
            spec,
            actor=pid,
            replace=replace,
            require_capability=False,
            source_type="workspace",
            source=read.path,
            manifest_sha256=self._hash_manifest(read.content),
        )
        result = self.load_skill(pid, spec.skill_id, actor=pid, require_capability=False)
        self.capabilities.consume_allow_once(pid, self.resource_for(spec.skill_id), CapabilityRight.WRITE, "skill")
        self.capabilities.consume_allow_once(pid, self.resource_for(spec.skill_id), CapabilityRight.EXECUTE, "skill")
        return {**result, "source": read.path, "registered": True}

    def discover_skills(
        self,
        text: str | None = None,
        *,
        actor: str | None = None,
        require_capability: bool = True,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if require_capability and actor is not None:
            self.capabilities.require(actor, self.config.skills.registry_resource, CapabilityRight.READ)
        selected_limit = self.config.skills.discover_limit if limit is None else limit
        return [self._skill_summary(skill, metadata) for skill, metadata in self.store.list_skills(text=text, limit=selected_limit)]

    def inspect_skill(
        self,
        skill_id: str,
        *,
        actor: str | None = None,
        require_capability: bool = True,
    ) -> dict[str, Any]:
        skill, metadata = self._get_skill(skill_id)
        if require_capability and actor is not None:
            self._require_skill_right(actor, skill_id, CapabilityRight.READ)
        return {
            **self._skill_summary(skill, metadata),
            "instructions": self._prompt_instructions(skill),
            "actions": [asdict(action) for action in skill.actions],
            "jit_tools": [self._jit_summary(tool) for tool in skill.jit_tools],
            "required_capabilities": list(skill.required_capabilities),
            "metadata": dict(skill.metadata),
        }

    def prompt_context(self, pid: str) -> list[dict[str, Any]]:
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        result: list[dict[str, Any]] = []
        for skill_id, loaded in process.loaded_skills.items():
            found = self.store.get_skill(skill_id)
            if found is None:
                result.append({"skill_id": skill_id, "missing": True, "loaded": loaded})
                continue
            skill, _metadata = found
            result.append(
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name,
                    "version": skill.version,
                    "description": skill.description,
                    "instructions": self._prompt_instructions(skill),
                    "tools": list(skill.tools),
                    "actions": [asdict(action) for action in skill.actions],
                    "jit_tools": [self._jit_summary(tool) for tool in skill.jit_tools],
                    "required_capabilities": list(skill.required_capabilities),
                    "loaded": loaded,
                }
            )
        return result

    def load_skill(
        self,
        pid: str,
        skill_id: str,
        *,
        actor: str | None = None,
        require_capability: bool = True,
    ) -> dict[str, Any]:
        selected_actor = actor or pid
        skill, metadata = self._get_skill(skill_id)
        if require_capability:
            self._require_skill_right(selected_actor, skill_id, CapabilityRight.EXECUTE)
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        self._validate_loadable(skill, process.tool_table)
        existing_handles = self._resolve_existing_tools(skill.tools)
        jit_handles = self._register_jit_tools(pid, skill)
        tool_ids = {name: handle.tool_id for name, handle in existing_handles.items()}
        jit_tool_ids = {name: handle.tool_id for name, handle in jit_handles.items()}
        updated_table = dict(process.tool_table)
        for name, handle in {**existing_handles, **jit_handles}.items():
            updated_table[name] = handle.tool_id
        loaded = LoadedSkill(
            skill_id=skill.skill_id,
            version=skill.version,
            source=metadata.get("source"),
            loaded_at=utc_now(),
            tool_names=sorted(updated_table_name for updated_table_name in [*tool_ids, *jit_tool_ids]),
            tool_ids=tool_ids,
            jit_tool_ids=jit_tool_ids,
            instructions_hash=self._hash_manifest(skill.instructions),
        )
        process.tool_table = updated_table
        process.loaded_skills[skill.skill_id] = to_jsonable(loaded)
        process.updated_at = utc_now()
        self.store.update_process(process)
        self.capabilities.consume_allow_once(selected_actor, self.resource_for(skill_id), CapabilityRight.EXECUTE, "skill")
        self.events.emit(
            EventType.SKILL_LOADED,
            source=selected_actor,
            target=pid,
            payload={"skill_id": skill.skill_id, "tool_names": loaded.tool_names},
        )
        self.audit.record(
            actor=selected_actor,
            action="skill.load",
            target=f"process:{pid}",
            decision={
                "skill_id": skill.skill_id,
                "version": skill.version,
                "tool_ids": tool_ids,
                "jit_tool_ids": jit_tool_ids,
                "source": metadata.get("source"),
            },
        )
        return {
            "pid": pid,
            "skill_id": skill.skill_id,
            "version": skill.version,
            "tool_names": loaded.tool_names,
            "tool_ids": tool_ids,
            "jit_tool_ids": jit_tool_ids,
            "instructions_hash": loaded.instructions_hash,
        }

    def unload_skill(
        self,
        pid: str,
        skill_id: str,
        *,
        actor: str | None = None,
        require_capability: bool = True,
    ) -> dict[str, Any]:
        selected_actor = actor or pid
        if require_capability:
            self._require_skill_right(selected_actor, skill_id, CapabilityRight.EXECUTE)
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        loaded = process.loaded_skills.get(skill_id)
        if loaded is None:
            raise NotFound(f"skill is not loaded in process {pid}: {skill_id}")
        tool_ids = dict(loaded.get("tool_ids", {})) if isinstance(loaded, dict) else {}
        jit_tool_ids = dict(loaded.get("jit_tool_ids", {})) if isinstance(loaded, dict) else {}
        removed: list[str] = []
        for name, tool_id in {**tool_ids, **jit_tool_ids}.items():
            if process.tool_table.get(name) == tool_id:
                process.tool_table.pop(name, None)
                removed.append(name)
        process.loaded_skills.pop(skill_id, None)
        process.updated_at = utc_now()
        self.store.update_process(process)
        self.capabilities.consume_allow_once(selected_actor, self.resource_for(skill_id), CapabilityRight.EXECUTE, "skill")
        self.events.emit(
            EventType.SKILL_UNLOADED,
            source=selected_actor,
            target=pid,
            payload={"skill_id": skill_id, "removed_tools": sorted(removed)},
        )
        self.audit.record(
            actor=selected_actor,
            action="skill.unload",
            target=f"process:{pid}",
            decision={"skill_id": skill_id, "removed_tools": sorted(removed)},
        )
        return {"pid": pid, "skill_id": skill_id, "removed_tools": sorted(removed)}

    def trust_skill_source(
        self,
        *,
        actor: str,
        source_type: str,
        source: str,
        manifest_sha256: str,
        require_capability: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected_source_type = self._validate_source_type(source_type)
        if require_capability:
            self.capabilities.require(actor, self.config.skills.trust_resource, CapabilityRight.ADMIN)
        self.store.insert_skill_trust(
            trust_id=new_id("strust"),
            source_type=selected_source_type,
            source=source,
            manifest_sha256=manifest_sha256,
            trusted_by=actor,
            created_at=utc_now(),
            metadata=metadata or {},
        )
        self.events.emit(
            EventType.SKILL_TRUSTED,
            source=actor,
            target=self.trust_resource(manifest_sha256),
            payload={"source_type": selected_source_type, "source": source},
        )
        self.audit.record(
            actor=actor,
            action="skill.trust",
            target=self.trust_resource(manifest_sha256),
            decision={"source_type": selected_source_type, "source": source, "manifest_sha256": manifest_sha256},
        )
        return {"source_type": selected_source_type, "source": source, "manifest_sha256": manifest_sha256, "trusted": True}

    def untrust_skill_source(
        self,
        *,
        actor: str,
        source_type: str,
        source: str,
        manifest_sha256: str,
        require_capability: bool = True,
    ) -> dict[str, Any]:
        selected_source_type = self._validate_source_type(source_type)
        if require_capability:
            self.capabilities.require(actor, self.config.skills.trust_resource, CapabilityRight.ADMIN)
        self.store.delete_skill_trust(source_type=selected_source_type, source=source, manifest_sha256=manifest_sha256)
        self.audit.record(
            actor=actor,
            action="skill.untrust",
            target=self.trust_resource(manifest_sha256),
            decision={"source_type": selected_source_type, "source": source, "manifest_sha256": manifest_sha256},
        )
        return {"source_type": selected_source_type, "source": source, "manifest_sha256": manifest_sha256, "trusted": False}

    def parse_manifest_text(self, text: str) -> SkillSpec:
        stripped = text.lstrip()
        if stripped.startswith("{"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"invalid skill JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise ValidationError("skill JSON document must be a mapping")
        else:
            data = load_yaml_mapping(text)
        if set(data) == {"skill"} and isinstance(data["skill"], dict):
            data = dict(data["skill"])
        return self._coerce_skill(data)

    def _coerce_skill(self, skill: SkillSpec | dict[str, Any]) -> SkillSpec:
        if isinstance(skill, SkillSpec):
            return skill
        if not isinstance(skill, dict):
            raise ValidationError("skill registration requires a SkillSpec or mapping")
        allowed = {
            "schema_version",
            "skill_id",
            "name",
            "version",
            "description",
            "instructions",
            "tools",
            "actions",
            "jit_tools",
            "required_capabilities",
            "metadata",
            "signature",
        }
        unknown = sorted(set(skill) - allowed)
        if unknown:
            raise ValidationError(f"unknown Skill fields: {unknown}")
        for field in ["schema_version", "skill_id", "name"]:
            if field not in skill:
                raise ValidationError(f"missing required Skill field: {field}")
        return SkillSpec(
            schema_version=self._require_int(skill["schema_version"], "schema_version"),
            skill_id=self._require_string(skill["skill_id"], "skill_id"),
            name=self._require_string(skill["name"], "name"),
            version=self._optional_string(skill.get("version"), "version") or "v0",
            description=self._optional_text(skill.get("description"), "description") or "",
            instructions=self._optional_text(skill.get("instructions"), "instructions") or "",
            tools=self._string_list(skill.get("tools"), "tools"),
            actions=[self._coerce_action(item) for item in self._list(skill.get("actions"), "actions")],
            jit_tools=[self._coerce_jit_tool(item) for item in self._list(skill.get("jit_tools"), "jit_tools")],
            required_capabilities=self._capability_specs(skill.get("required_capabilities")),
            metadata=self._mapping(skill.get("metadata"), "metadata"),
            signature=self._optional_string(skill.get("signature"), "signature"),
        )

    def _validate_skill(self, skill: SkillSpec) -> None:
        defaults = self.config.skills
        if skill.schema_version != defaults.schema_version:
            raise ValidationError(f"unsupported Skill schema_version: {skill.schema_version}")
        self._validate_identifier(skill.skill_id, "skill_id", defaults.id_max_chars)
        self._validate_string_length(skill.name, "name", defaults.name_max_chars)
        self._validate_string_length(skill.version, "version", defaults.version_max_chars)
        if len(skill.instructions) > defaults.max_prompt_instruction_chars:
            raise ValidationError(f"instructions exceeds max_prompt_instruction_chars={defaults.max_prompt_instruction_chars}")
        if len(skill.tools) > defaults.max_tools:
            raise ValidationError(f"tools exceeds max_tools={defaults.max_tools}")
        if len(skill.actions) > defaults.max_actions:
            raise ValidationError(f"actions exceeds max_actions={defaults.max_actions}")
        if len(skill.jit_tools) > defaults.max_jit_tools:
            raise ValidationError(f"jit_tools exceeds max_jit_tools={defaults.max_jit_tools}")
        if len(skill.required_capabilities) > defaults.max_required_capabilities:
            raise ValidationError(f"required_capabilities exceeds max_required_capabilities={defaults.max_required_capabilities}")
        names = [*skill.tools, *(tool.name for tool in skill.jit_tools)]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValidationError(f"duplicate Skill tool names: {duplicates}")
        for tool in skill.jit_tools:
            self._validate_identifier(tool.name, "jit_tools[].name", defaults.id_max_chars)
            if len(tool.source) > defaults.max_jit_source_chars:
                raise ValidationError(f"JIT source for {tool.name} exceeds max_jit_source_chars={defaults.max_jit_source_chars}")
        for spec in skill.required_capabilities:
            self._validate_capability_spec(spec)

    def _validate_loadable(self, skill: SkillSpec, process_tool_table: dict[str, str]) -> None:
        runtime = self._runtime()
        static_names = {
            row["name"]
            for row in runtime.tools.list()
            if not bool(row.get("ephemeral"))
        }
        for name in skill.tools:
            runtime.tools.resolve(name)
        for tool in skill.jit_tools:
            if tool.name in process_tool_table:
                raise ValidationError(f"process already has a tool named: {tool.name}")
            if tool.name in static_names:
                raise ValidationError(f"JIT skill tool cannot shadow static tool: {tool.name}")
            result = runtime.tools.sandbox.run_tests(tool.source, tool.tests)
            if not result.ok or result.errors:
                raise ValidationError(f"JIT skill tool {tool.name} failed validation: {'; '.join(result.errors)}")

    def _resolve_existing_tools(self, names: list[str]) -> dict[str, Any]:
        runtime = self._runtime()
        return {name: runtime.tools.resolve(name) for name in names}

    def _register_jit_tools(self, pid: str, skill: SkillSpec) -> dict[str, Any]:
        runtime = self._runtime()
        handles: dict[str, Any] = {}
        for jit in skill.jit_tools:
            candidate_id = runtime.tools.propose(
                pid,
                {
                    "name": jit.name,
                    "description": jit.description,
                    "input_schema": jit.input_schema,
                    "output_schema": jit.output_schema,
                    "metadata": {"skill_id": skill.skill_id, **jit.metadata},
                },
                source_code=jit.source,
                tests=jit.tests,
            )
            validation = runtime.tools.validate(candidate_id, pid=pid)
            if not validation.ok:
                raise ValidationError(f"JIT skill tool {jit.name} failed validation: {'; '.join(validation.errors)}")
            candidate = runtime.store.get_tool_candidate(candidate_id)
            if candidate is None:
                raise NotFound(f"tool candidate not found after validation: {candidate_id}")
            candidate.status = ToolCandidateStatus.VALIDATED
            runtime.store.update_tool_candidate(candidate)
            handles[jit.name] = runtime.tools.register(pid, candidate_id, approver=f"skill:{skill.skill_id}")
        return handles

    def _require_skill_right(self, actor: str, skill_id: str, right: CapabilityRight) -> None:
        self._require_skill_rights(actor, skill_id, [right])

    def _require_skill_rights(self, actor: str, skill_id: str, rights: Iterable[CapabilityRight]) -> None:
        resource = self.resource_for(skill_id)
        missing: list[str] = []
        for right in rights:
            policy = self.capabilities.permission_policy(actor, resource, right)
            if policy in {CapabilityManager.ALWAYS_ALLOW, CapabilityManager.ALLOW_ONCE}:
                continue
            missing.append(str(right))
        if not missing:
            return
        if self.human is None:
            raise CapabilityDenied(f"{actor} lacks {missing} on {resource}")
        request_id = self.human.query(
            pid=actor,
            human=self.config.runtime.default_human,
            request={
                "type": "permission_request",
                "question": f"Allow process {actor} to use skill {skill_id} rights={missing} once?",
                "requested_once_capability": {
                    "subject": actor,
                    "resource": resource,
                    "rights": missing,
                },
                "context": {"primitive": "skill", "skill_id": skill_id},
            },
            blocking=True,
        )
        raise HumanApprovalRequired(request_id, f"human approval required for skill {skill_id}")

    def _require_trusted_global_source(self, source: str, manifest_sha256: str) -> None:
        if not self.config.skills.global_requires_trust:
            return
        if manifest_sha256 in set(self.config.skills.trusted_global_sha256):
            return
        if self.store.is_skill_trusted(source_type="global", source=source, manifest_sha256=manifest_sha256):
            return
        raise CapabilityDenied(f"global skill source is not trusted: {source} sha256={manifest_sha256}")

    def _normalize_global_path(self, path: str | Path) -> tuple[Path, str]:
        selected = Path(path).expanduser()
        if not selected.is_absolute():
            selected = Path.cwd() / selected
        selected = selected.resolve()
        roots = [Path(root).expanduser().resolve() for root in self.config.skills.global_dirs]
        for root in roots:
            try:
                relative = selected.relative_to(root)
            except ValueError:
                continue
            return selected, relative.as_posix()
        raise CapabilityDenied(f"global skill path is outside configured global_dirs: {selected}")

    def _read_text_limited(self, path: Path) -> str:
        if not path.exists() or not path.is_file():
            raise NotFound(f"skill manifest not found: {path}")
        raw = path.read_bytes()
        if len(raw) > self.config.skills.manifest_hard_limit_bytes:
            raise ValidationError(f"skill manifest exceeds manifest_hard_limit_bytes={self.config.skills.manifest_hard_limit_bytes}")
        return raw.decode("utf-8")

    def _check_manifest_size(self, text: str) -> None:
        size = len(text.encode("utf-8"))
        if size > self.config.skills.manifest_hard_limit_bytes:
            raise ValidationError(f"skill manifest exceeds manifest_hard_limit_bytes={self.config.skills.manifest_hard_limit_bytes}")
        if size > self.config.skills.manifest_max_bytes:
            raise ValidationError(f"skill manifest exceeds manifest_max_bytes={self.config.skills.manifest_max_bytes}")

    def _get_skill(self, skill_id: str) -> tuple[SkillSpec, dict[str, Any]]:
        found = self.store.get_skill(skill_id)
        if found is None:
            raise NotFound(f"skill not found: {skill_id}")
        return found

    def _skill_summary(self, skill: SkillSpec, metadata: dict[str, Any]) -> dict[str, Any]:
        return {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "version": skill.version,
            "description": skill.description,
            "tools": list(skill.tools),
            "actions": [action.name for action in skill.actions],
            "jit_tools": [tool.name for tool in skill.jit_tools],
            "required_capabilities": list(skill.required_capabilities),
            "source_type": metadata.get("source_type"),
            "source": metadata.get("source"),
            "manifest_sha256": metadata.get("manifest_sha256"),
            "signed": bool(metadata.get("signed")),
        }

    def _jit_summary(self, tool: JitToolSpec) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "tests": tool.tests,
            "source_sha256": self._hash_manifest(tool.source),
        }

    def _prompt_instructions(self, skill: SkillSpec) -> str:
        return skill.instructions[: self.config.skills.max_prompt_instruction_chars]

    def _hash_manifest(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _validate_source_type(self, source_type: str) -> str:
        if source_type not in _SOURCE_TYPES:
            raise ValidationError(f"unsupported skill source_type: {source_type}")
        return source_type

    def _runtime(self) -> Any:
        if self.runtime is None:
            raise RuntimeError("SkillManager is not bound to a Runtime")
        return self.runtime

    def _coerce_action(self, value: Any) -> ActionSchema:
        if not isinstance(value, dict):
            raise ValidationError("actions entries must be mappings")
        allowed = {
            "name",
            "use_cases",
            "input_schema",
            "output_schema",
            "required_capabilities",
            "side_effects",
            "failure_modes",
            "examples",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValidationError(f"unknown Skill action fields: {unknown}")
        examples: list[dict[str, Any]] = []
        for item in self._list(value.get("examples"), "actions[].examples"):
            if not isinstance(item, dict):
                raise ValidationError("actions[].examples entries must be mappings")
            examples.append(dict(item))
        return ActionSchema(
            name=self._require_string(value.get("name"), "actions[].name"),
            use_cases=self._string_list(value.get("use_cases"), "actions[].use_cases"),
            input_schema=self._mapping(value.get("input_schema"), "actions[].input_schema"),
            output_schema=self._mapping(value.get("output_schema"), "actions[].output_schema"),
            required_capabilities=self._capability_specs(value.get("required_capabilities")),
            side_effects=self._string_list(value.get("side_effects"), "actions[].side_effects"),
            failure_modes=self._string_list(value.get("failure_modes"), "actions[].failure_modes"),
            examples=examples,
        )

    def _coerce_jit_tool(self, value: Any) -> JitToolSpec:
        if not isinstance(value, dict):
            raise ValidationError("jit_tools entries must be mappings")
        allowed = {
            "name",
            "description",
            "input_schema",
            "output_schema",
            "source",
            "tests",
            "metadata",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValidationError(f"unknown Skill JIT tool fields: {unknown}")
        source = self._optional_text(value.get("source"), "jit_tools[].source")
        if source is None:
            raise ValidationError("jit_tools entries require inline source")
        tests: list[dict[str, Any]] = []
        for item in self._list(value.get("tests"), "jit_tools[].tests"):
            if not isinstance(item, dict):
                raise ValidationError("jit_tools[].tests entries must be mappings")
            tests.append(dict(item))
        return JitToolSpec(
            name=self._require_string(value.get("name"), "jit_tools[].name"),
            description=self._require_string(value.get("description"), "jit_tools[].description"),
            input_schema=self._mapping(value.get("input_schema"), "jit_tools[].input_schema"),
            output_schema=self._mapping(value.get("output_schema"), "jit_tools[].output_schema"),
            source=source,
            tests=tests,
            metadata=self._mapping(value.get("metadata"), "jit_tools[].metadata"),
        )

    def _list(self, value: Any, field: str) -> list[Any]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValidationError(f"{field} must be a list")
        return list(value)

    def _string_list(self, value: Any, field: str) -> list[str]:
        return [self._require_string(item, f"{field}[]") for item in self._list(value, field)]

    def _mapping(self, value: Any, field: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValidationError(f"{field} must be a mapping")
        return dict(value)

    def _capability_specs(self, value: Any) -> list[dict[str, Any]]:
        specs = self._list(value, "required_capabilities")
        normalized: list[dict[str, Any]] = []
        for spec in specs:
            if not isinstance(spec, dict):
                raise ValidationError("capability spec entries must be mappings")
            item = dict(spec)
            self._validate_capability_spec(item)
            normalized.append(item)
        return normalized

    def _validate_capability_spec(self, spec: dict[str, Any]) -> None:
        resource = spec.get("resource")
        rights = spec.get("rights")
        if not isinstance(resource, str) or not resource:
            raise ValidationError("capability spec requires a non-empty resource")
        if not isinstance(rights, list) or not rights or not all(isinstance(right, str) and right for right in rights):
            raise ValidationError("capability spec requires a non-empty rights list")
        constraints = spec.get("constraints")
        if constraints is not None and not isinstance(constraints, dict):
            raise ValidationError("capability spec constraints must be a mapping")

    def _require_string(self, value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{field} must be a non-empty string")
        return value.strip()

    def _optional_string(self, value: Any, field: str) -> str | None:
        if value is None:
            return None
        return self._require_string(value, field)

    def _optional_text(self, value: Any, field: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValidationError(f"{field} must be a string")
        return value

    def _require_int(self, value: Any, field: str) -> int:
        if isinstance(value, bool):
            raise ValidationError(f"{field} must be an integer")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} must be an integer") from exc

    def _validate_identifier(self, value: str, field: str, max_chars: int) -> None:
        self._validate_string_length(value, field, max_chars)
        if not _SKILL_ID_PATTERN.match(value):
            raise ValidationError(f"{field} contains unsupported characters: {value!r}")

    def _validate_string_length(self, value: str, field: str, max_chars: int) -> None:
        if len(value) > max_chars:
            raise ValidationError(f"{field} exceeds max length {max_chars}")
        if any(ord(char) < 32 for char in value):
            raise ValidationError(f"{field} contains control characters")

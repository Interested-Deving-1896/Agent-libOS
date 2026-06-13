from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models import AgentImage, Capability, CapabilityRight, EventType
from agent_libos.models.exceptions import CapabilityDenied, NotFound, ValidationError
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.storage import SQLiteStore
from agent_libos.utils.ids import utc_now
from agent_libos.utils.serde import dumps, loads
from agent_libos.utils.yaml_loader import load_yaml_mapping

_IMAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]*$")
@dataclass(frozen=True)
class ImageRegistrationResult:
    image: AgentImage
    replaced: bool
    source: str | None = None


class ImageRegistryPrimitive:
    """Registers AgentImage definitions under capability, audit, and event control."""

    IMAGE_FIELDS = {
        "image_id",
        "name",
        "version",
        "system_prompt",
        "planner",
        "action_schema",
        "default_skills",
        "default_tools",
        "context_policy",
        "safety_profile",
        "required_capabilities",
        "metadata",
        "signature",
        "boot",
    }

    def __init__(
        self,
        images: dict[str, AgentImage],
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        tool_exists: Any,
        store: SQLiteStore | None = None,
        config: AgentLibOSConfig | None = None,
    ):
        self.config = config or DEFAULT_CONFIG
        self.store = store
        self.images = images
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        self.tool_exists = tool_exists
        self.runtime: Any | None = None

    def bind_runtime(self, runtime: Any) -> None:
        self.runtime = runtime

    def register(
        self,
        image: AgentImage | dict[str, Any],
        *,
        actor: str = "runtime",
        replace: bool = False,
        require_capability: bool = False,
        source: str | None = None,
    ) -> ImageRegistrationResult:
        candidate = self._coerce_image(image)
        if require_capability:
            self.capabilities.require(actor, self.resource_for(candidate.image_id), CapabilityRight.WRITE)
        existing = self.images.get(candidate.image_id)
        if existing is not None and not replace:
            raise ValidationError(f"agent image already exists: {candidate.image_id}")
        self._validate_image(candidate)
        self.images[candidate.image_id] = candidate
        now = utc_now()
        if self.store is not None:
            self.store.upsert_image(candidate, registered_by=actor, source=source, created_at=now)
        action = "image.replace" if existing is not None else "image.register"
        self.events.emit(
            EventType.IMAGE_REGISTERED,
            source=actor,
            target=self.resource_for(candidate.image_id),
            payload={
                "image_id": candidate.image_id,
                "name": candidate.name,
                "version": candidate.version,
                "replaced": existing is not None,
                "source": source,
                "boot_kind": candidate.boot.get("kind", "fresh"),
            },
        )
        self.audit.record(
            actor=actor,
            action=action,
            target=self.resource_for(candidate.image_id),
            decision={
                "image_id": candidate.image_id,
                "name": candidate.name,
                "version": candidate.version,
                "default_tools": list(candidate.default_tools),
                "required_capabilities": len(candidate.required_capabilities),
                "replaced": existing is not None,
                "source": source,
                "boot_kind": candidate.boot.get("kind", "fresh"),
            },
        )
        return ImageRegistrationResult(image=candidate, replaced=existing is not None, source=source)

    def register_from_yaml_text(
        self,
        text: str,
        *,
        actor: str,
        replace: bool = False,
        require_capability: bool = False,
        source: str | None = None,
    ) -> ImageRegistrationResult:
        data = load_yaml_mapping(text)
        if set(data) == {"image"} and isinstance(data["image"], dict):
            data = data["image"]
        return self.register(
            data,
            actor=actor,
            replace=replace,
            require_capability=require_capability,
            source=source,
        )

    def load_persisted_images(self) -> None:
        if self.store is None:
            return
        for image, _metadata in self.store.list_images():
            # Persisted images may depend on startup modules that are not loaded
            # in this Runtime.open() invocation. Keep the manifest inspectable
            # and defer concrete tool resolution to spawn/exec.
            self._validate_image(image, validate_tools=False)
            self.images[image.image_id] = image

    def list_images(self) -> list[dict[str, Any]]:
        if self.store is None:
            return [self._image_summary(image, {}) for image in sorted(self.images.values(), key=lambda item: item.image_id)]
        return [self._image_summary(image, metadata) for image, metadata in self.store.list_images()]

    def inspect(self, image_id: str) -> dict[str, Any]:
        image = self.images.get(image_id)
        metadata: dict[str, Any] = {}
        if self.store is not None:
            persisted = self.store.get_image(image_id)
            if persisted is not None:
                image, metadata = persisted
        if image is None:
            raise NotFound(f"agent image not found: {image_id}")
        artifact = None
        boot = image.boot or {"kind": "fresh"}
        if boot.get("kind") == "checkpoint_commit" and self.store is not None:
            found = self.store.get_image_artifact(str(boot.get("artifact_id")))
            if found is not None:
                artifact_data, artifact_meta = found
                artifact = {
                    **artifact_meta,
                    "source_checkpoint_id": artifact_data.get("source_checkpoint_id"),
                    "source_pid": artifact_data.get("source_pid"),
                    "counts": artifact_data.get("counts", {}),
                    "modules": artifact_data.get("modules", []),
                }
        return {
            "image": self._image_to_dict(image),
            "registry": metadata,
            "artifact": artifact,
        }

    def commit_from_checkpoint(
        self,
        *,
        actor: str,
        checkpoint_id: str,
        image_id: str,
        name: str,
        version: str = "v0",
        replace: bool = False,
        metadata: dict[str, Any] | None = None,
        require_capability: bool = True,
    ) -> ImageRegistrationResult:
        if self.runtime is None or self.store is None:
            raise ValidationError("checkpoint image commit requires a bound Runtime and SQLiteStore")
        found = self.store.get_checkpoint_snapshot(checkpoint_id)
        if found is None:
            raise NotFound(f"checkpoint not found: {checkpoint_id}")
        checkpoint, snapshot = found
        if require_capability:
            self.capabilities.require(actor, self.resource_for(image_id), CapabilityRight.WRITE)
            self.runtime.checkpoint._require_checkpoint_or_process_read(actor, checkpoint)
        self.runtime.checkpoint._require_snapshot_modules(snapshot)
        self._validate_identifier(image_id, "image_id", self.config.image.id_max_chars)
        self._validate_string_length(name, "name", self.config.image.name_max_chars)
        self._validate_string_length(version, "version", self.config.image.version_max_chars)
        if image_id in self.images and not replace:
            raise ValidationError(f"agent image already exists: {image_id}")
        artifact = self._build_commit_artifact(snapshot, checkpoint_id=checkpoint_id)
        artifact_json = dumps(artifact)
        artifact_bytes = len(artifact_json.encode("utf-8"))
        if artifact_bytes > self.config.image_commit.artifact_hard_limit_bytes:
            raise ValidationError(
                "image artifact exceeded "
                f"artifact_hard_limit_bytes={self.config.image_commit.artifact_hard_limit_bytes}"
            )
        artifact_sha256 = hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()
        artifact_id = f"imgart_{artifact_sha256[:24]}"
        created_at = utc_now()
        if self.store.get_image_artifact(artifact_id) is None:
            self.store.insert_image_artifact(
                artifact_id=artifact_id,
                kind="checkpoint_commit",
                artifact=artifact,
                sha256=artifact_sha256,
                created_by=actor,
                created_at=created_at,
                metadata={
                    "source_checkpoint_id": checkpoint_id,
                    "source_pid": checkpoint.pid,
                    "artifact_bytes": artifact_bytes,
                },
            )
        source_image = self.images.get(str(artifact["source_image_id"]))
        image = AgentImage(
            image_id=image_id,
            name=name,
            version=version,
            system_prompt=source_image.system_prompt if source_image is not None else "",
            planner=dict(source_image.planner) if source_image is not None else {},
            action_schema=dict(source_image.action_schema) if source_image is not None else {},
            default_skills=list(artifact.get("default_skills", [])),
            default_tools=list(artifact.get("static_default_tools", [])),
            context_policy=source_image.context_policy if source_image is not None else "plan_first",
            safety_profile=source_image.safety_profile if source_image is not None else "default",
            required_capabilities=self._dedupe_capability_specs(artifact.get("required_capabilities", [])),
            metadata={
                **(metadata or {}),
                "committed_from_checkpoint": checkpoint_id,
                "committed_from_pid": checkpoint.pid,
                "artifact_sha256": artifact_sha256,
                "artifact_bytes": artifact_bytes,
                "commit_kind": "checkpoint_commit",
            },
            boot={
                "kind": "checkpoint_commit",
                "artifact_id": artifact_id,
                "artifact_sha256": artifact_sha256,
                "source_checkpoint_id": checkpoint_id,
                "source_pid": checkpoint.pid,
                "root_only": True,
            },
        )
        result = self.register(
            image,
            actor=actor,
            replace=replace,
            require_capability=False,
            source=f"checkpoint:{checkpoint_id}",
        )
        self.events.emit(
            EventType.IMAGE_COMMITTED,
            source=actor,
            target=self.resource_for(image_id),
            payload={
                "image_id": image_id,
                "checkpoint_id": checkpoint_id,
                "artifact_id": artifact_id,
                "artifact_sha256": artifact_sha256,
                "artifact_bytes": artifact_bytes,
            },
        )
        self.audit.record(
            actor=actor,
            action="image.commit",
            target=self.resource_for(image_id),
            decision={
                "checkpoint_id": checkpoint_id,
                "source_pid": checkpoint.pid,
                "artifact_id": artifact_id,
                "artifact_sha256": artifact_sha256,
                "artifact_bytes": artifact_bytes,
                "required_capabilities": len(image.required_capabilities),
            },
        )
        return result

    def grant_register(
        self,
        pid: str,
        image_id: str = "*",
        issued_by: str = "image_registry",
    ) -> Capability:
        resource = self.config.image.registry_resource if image_id == "*" else self.resource_for(image_id)
        return self.capabilities.grant(
            subject=pid,
            resource=resource,
            rights=[CapabilityRight.WRITE],
            issued_by=issued_by,
        )

    def resource_for(self, image_id: str) -> str:
        return f"image:{image_id}"

    def registry_resource(self) -> str:
        return self.config.image.registry_resource

    def _coerce_image(self, image: AgentImage | dict[str, Any]) -> AgentImage:
        if isinstance(image, AgentImage):
            return image
        if not isinstance(image, dict):
            raise ValidationError("image registration requires an AgentImage or mapping")
        unknown = sorted(set(image) - self.IMAGE_FIELDS)
        if unknown:
            raise ValidationError(f"unknown AgentImage fields: {unknown}")
        required = {"image_id", "name"}
        missing = sorted(key for key in required if key not in image)
        if missing:
            raise ValidationError(f"missing required AgentImage fields: {missing}")
        return AgentImage(
            image_id=self._require_string(image["image_id"], "image_id"),
            name=self._require_string(image["name"], "name"),
            version=self._optional_string(image.get("version"), "version") or "v0",
            system_prompt=self._optional_text(image.get("system_prompt"), "system_prompt") or "",
            planner=self._mapping(image.get("planner"), "planner"),
            action_schema=self._mapping(image.get("action_schema"), "action_schema"),
            default_skills=self._string_list(image.get("default_skills"), "default_skills"),
            default_tools=self._string_list(image.get("default_tools"), "default_tools"),
            context_policy=self._optional_string(image.get("context_policy"), "context_policy") or "plan_first",
            safety_profile=self._optional_string(image.get("safety_profile"), "safety_profile") or "default",
            required_capabilities=self._capability_specs(image.get("required_capabilities")),
            metadata=self._mapping(image.get("metadata"), "metadata"),
            signature=self._optional_string(image.get("signature"), "signature"),
            boot=self._boot_mapping(image.get("boot")),
        )

    def _validate_image(self, image: AgentImage, *, validate_tools: bool = True) -> None:
        self._validate_identifier(image.image_id, "image_id", self.config.image.id_max_chars)
        self._validate_string_length(image.name, "name", self.config.image.name_max_chars)
        self._validate_string_length(image.version, "version", self.config.image.version_max_chars)
        if len(image.default_tools) > self.config.image.max_default_tools:
            raise ValidationError(f"default_tools exceeds max_default_tools={self.config.image.max_default_tools}")
        if len(image.default_skills) > self.config.skills.max_tools:
            raise ValidationError(f"default_skills exceeds max_tools={self.config.skills.max_tools}")
        if len(image.required_capabilities) > self.config.image.max_required_capabilities:
            raise ValidationError(
                "required_capabilities exceeds "
                f"max_required_capabilities={self.config.image.max_required_capabilities}"
            )
        for skill_id in image.default_skills:
            self._validate_identifier(skill_id, "default_skills[]", self.config.skills.id_max_chars)
        for tool_name in image.default_tools:
            self._validate_identifier(tool_name, "default_tools[]", self.config.image.id_max_chars)
            if not validate_tools:
                continue
            try:
                self.tool_exists(tool_name)
            except Exception as exc:
                raise ValidationError(f"unknown tool in AgentImage default_tools: {tool_name}") from exc
        for spec in image.required_capabilities:
            self._validate_capability_spec(spec)
        self._validate_boot(image.boot)

    def _validate_identifier(self, value: str, field: str, max_chars: int) -> None:
        self._validate_string_length(value, field, max_chars)
        if not _IMAGE_ID_PATTERN.match(value):
            raise ValidationError(f"{field} contains unsupported characters: {value!r}")

    def _validate_string_length(self, value: str, field: str, max_chars: int) -> None:
        if not isinstance(value, str) or not value:
            raise ValidationError(f"{field} must be a non-empty string")
        if len(value) > max_chars:
            raise ValidationError(f"{field} exceeds max length {max_chars}")
        if any(ord(char) < 32 for char in value):
            raise ValidationError(f"{field} contains control characters")

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

    def _string_list(self, value: Any, field: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValidationError(f"{field} must be a list")
        return [self._require_string(item, f"{field}[]") for item in value]

    def _mapping(self, value: Any, field: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValidationError(f"{field} must be a mapping")
        return dict(value)

    def _boot_mapping(self, value: Any) -> dict[str, Any]:
        if value is None:
            return {"kind": "fresh"}
        if not isinstance(value, dict):
            raise ValidationError("boot must be a mapping")
        return dict(value)

    def _validate_boot(self, boot: dict[str, Any]) -> None:
        kind = boot.get("kind", "fresh")
        if kind not in {"fresh", "checkpoint_commit"}:
            raise ValidationError(f"unsupported image boot kind: {kind}")
        if kind == "fresh":
            return
        artifact_id = boot.get("artifact_id")
        artifact_sha256 = boot.get("artifact_sha256")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValidationError("checkpoint_commit boot requires artifact_id")
        if not isinstance(artifact_sha256, str) or not artifact_sha256:
            raise ValidationError("checkpoint_commit boot requires artifact_sha256")

    def _capability_specs(self, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValidationError("required_capabilities must be a list")
        specs: list[dict[str, Any]] = []
        for spec in value:
            if not isinstance(spec, dict):
                raise ValidationError("required_capabilities entries must be mappings")
            normalized = dict(spec)
            self._validate_capability_spec(normalized)
            specs.append(normalized)
        return specs

    def _validate_capability_spec(self, spec: dict[str, Any]) -> None:
        resource = spec.get("resource")
        if not isinstance(resource, str) or not resource:
            raise ValidationError("capability spec requires a non-empty resource")
        try:
            self.capabilities.parse_resource_pattern(resource)
        except CapabilityDenied as exc:
            raise ValidationError(str(exc)) from exc
        rights = spec.get("rights")
        if not isinstance(rights, list) or not rights or not all(isinstance(right, str) and right for right in rights):
            raise ValidationError("capability spec requires a non-empty rights list")
        for right in rights:
            try:
                CapabilityRight(str(right))
            except ValueError as exc:
                raise ValidationError(f"unknown capability right: {right}") from exc
        constraints = spec.get("constraints")
        if constraints is not None and not isinstance(constraints, dict):
            raise ValidationError("capability spec constraints must be a mapping")

    def _build_commit_artifact(self, snapshot: dict[str, Any], *, checkpoint_id: str) -> dict[str, Any]:
        source_pid = str(snapshot["pid"])
        process_rows = [row for row in snapshot.get("rows", {}).get("processes", []) if row["pid"] == source_pid]
        if not process_rows:
            raise ValidationError(f"checkpoint root process row is missing: {source_pid}")
        process_row = dict(process_rows[0])
        object_oids = self._root_object_oids(snapshot, process_row, source_pid)
        object_rows = [row for row in snapshot.get("rows", {}).get("objects", []) if row["oid"] in object_oids]
        namespace_names = self._root_namespace_names(snapshot, object_rows, source_pid)
        tool_table = loads(process_row.get("tool_table_json"), {})
        source_image_id = str(process_row.get("image_id"))
        source_image = self.images.get(source_image_id)
        static_tool_names = self._static_tool_names_for_commit(tool_table)
        if len(tool_table) > self.config.image_commit.max_committed_tools:
            raise ValidationError(
                f"committed tool table exceeds max_committed_tools={self.config.image_commit.max_committed_tools}"
            )
        required_capabilities, internal_capabilities = self._split_commit_capabilities(
            snapshot.get("rows", {}).get("capabilities", []),
            source_pid=source_pid,
            object_oids=object_oids,
            namespace_names=namespace_names,
        )
        object_payloads: dict[str, Any] = {}
        for oid in object_oids:
            payload = deepcopy(snapshot.get("object_payloads", {}).get(oid))
            payload_bytes = len(dumps(payload).encode("utf-8"))
            if payload_bytes > self.config.image_commit.payload_capture_limit_bytes:
                raise ValidationError(
                    f"object payload {oid} exceeds image_commit.payload_capture_limit_bytes="
                    f"{self.config.image_commit.payload_capture_limit_bytes}"
                )
            object_payloads[oid] = payload
        visible_tool_ids = set(tool_table.values())
        jit_sources = {
            tool_id: source
            for tool_id, source in snapshot.get("jit_sources", {}).items()
            if tool_id in visible_tool_ids
        }
        if len(jit_sources) > self.config.image_commit.max_committed_jit_sources:
            raise ValidationError(
                "committed JIT sources exceed "
                f"max_committed_jit_sources={self.config.image_commit.max_committed_jit_sources}"
            )
        return {
            "artifact_version": self.config.image_commit.artifact_version,
            "kind": "checkpoint_commit",
            "source_checkpoint_id": checkpoint_id,
            "source_pid": source_pid,
            "source_image_id": source_image_id,
            "source_process": process_row,
            "rows": {
                "object_namespaces": [
                    row for row in snapshot.get("rows", {}).get("object_namespaces", [])
                    if row["namespace"] in namespace_names
                ],
                "objects": object_rows,
                "object_links": [
                    row for row in snapshot.get("rows", {}).get("object_links", [])
                    if row["src_oid"] in object_oids and row["dst_oid"] in object_oids
                ],
                "capabilities": internal_capabilities,
                "skills": snapshot.get("rows", {}).get("skills", []),
                "skill_trust": snapshot.get("rows", {}).get("skill_trust", []),
                "tools": [
                    row for row in snapshot.get("rows", {}).get("tools", [])
                    if row["tool_id"] in visible_tool_ids
                ],
                "tool_candidates": [
                    row for row in snapshot.get("rows", {}).get("tool_candidates", [])
                    if row["pid"] == source_pid
                ],
                "jsonrpc_endpoints": snapshot.get("rows", {}).get("jsonrpc_endpoints", []),
            },
            "object_oids": sorted(object_oids),
            "namespaces": sorted(namespace_names),
            "object_payloads": object_payloads,
            "tool_table": tool_table,
            "loaded_skills": loads(process_row.get("loaded_skills_json"), {}),
            "jit_sources": jit_sources,
            "working_directory": process_row.get("working_directory", "."),
            "default_skills": list(source_image.default_skills) if source_image is not None else [],
            "static_default_tools": static_tool_names,
            "required_capabilities": required_capabilities,
            "modules": list(snapshot.get("modules", [])),
            "counts": {
                "objects": len(object_rows),
                "namespaces": len(namespace_names),
                "internal_capabilities": len(internal_capabilities),
                "required_capabilities": len(required_capabilities),
                "tools": len(tool_table),
                "jit_sources": len(jit_sources),
            },
        }

    def _root_object_oids(self, snapshot: dict[str, Any], process_row: dict[str, Any], source_pid: str) -> set[str]:
        oids: set[str] = set()
        if process_row.get("goal_oid"):
            oids.add(str(process_row["goal_oid"]))
        view = loads(process_row.get("memory_view_json"), {}) if process_row.get("memory_view_json") else {}
        for root in view.get("roots", []):
            if isinstance(root, dict) and root.get("oid"):
                oids.add(str(root["oid"]))
        for row in snapshot.get("rows", {}).get("objects", []):
            if row.get("created_by") == source_pid:
                oids.add(str(row["oid"]))
        available = set(snapshot.get("object_payloads", {}).keys())
        return {oid for oid in oids if oid in available}

    def _root_namespace_names(self, snapshot: dict[str, Any], object_rows: list[dict[str, Any]], source_pid: str) -> set[str]:
        process_namespace = f"{self.config.memory.process_namespace_prefix}:{source_pid}"
        names = {process_namespace}
        names.update(str(row["namespace"]) for row in object_rows)
        for row in snapshot.get("rows", {}).get("object_namespaces", []):
            if row.get("created_by") == source_pid or row.get("namespace") in names:
                names.add(str(row["namespace"]))
        return names

    def _split_commit_capabilities(
        self,
        rows: list[dict[str, Any]],
        *,
        source_pid: str,
        object_oids: set[str],
        namespace_names: set[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        required: list[dict[str, Any]] = []
        internal: list[dict[str, Any]] = []
        for row in rows:
            if row.get("subject") != source_pid or row.get("status") != "active":
                continue
            resource = str(row.get("resource") or "")
            rights = loads(row.get("rights_json"), [])
            if self._is_internal_commit_capability(resource, object_oids, namespace_names):
                internal.append(dict(row))
            else:
                # Unknown provider resource kinds are still external authority.
                # Preserve them as declarations, never as live boot grants.
                required.append(
                    {
                        "resource": resource,
                        "rights": rights,
                        "constraints": loads(row.get("constraints_json"), {}),
                    }
                )
        return self._dedupe_capability_specs(required), internal

    def _is_internal_commit_capability(self, resource: str, object_oids: set[str], namespace_names: set[str]) -> bool:
        if resource.startswith("object:"):
            return resource.split(":", 1)[1] in object_oids
        if resource.startswith("object_namespace:"):
            return resource.split(":", 1)[1] in namespace_names
        return False

    def _static_tool_names_for_commit(self, tool_table: dict[str, str]) -> list[str]:
        static: list[str] = []
        runtime = self.runtime
        tools = getattr(runtime, "tools", None)
        if tools is None:
            return static
        jit_sources = getattr(tools, "_jit_sources", {})
        for name, tool_id in sorted(tool_table.items()):
            if tool_id in jit_sources:
                continue
            try:
                tools.resolve(name)
            except Exception as exc:
                raise ValidationError(f"committed image references unavailable static tool: {name}") from exc
            static.append(name)
        return static

    def _dedupe_capability_specs(self, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {}
        for spec in specs:
            normalized = {
                "resource": spec["resource"],
                "rights": sorted(str(right) for right in spec.get("rights", [])),
            }
            constraints = spec.get("constraints") or {}
            if constraints:
                normalized["constraints"] = constraints
            key = dumps(normalized)
            by_key[key] = normalized
        result = list(by_key.values())
        if len(result) > self.config.image_commit.max_required_capabilities:
            raise ValidationError(
                "committed required_capabilities exceeds "
                f"max_required_capabilities={self.config.image_commit.max_required_capabilities}"
            )
        return result

    def _image_to_dict(self, image: AgentImage) -> dict[str, Any]:
        return {
            "image_id": image.image_id,
            "name": image.name,
            "version": image.version,
            "system_prompt": image.system_prompt,
            "planner": image.planner,
            "action_schema": image.action_schema,
            "default_skills": list(image.default_skills),
            "default_tools": list(image.default_tools),
            "context_policy": image.context_policy,
            "safety_profile": image.safety_profile,
            "required_capabilities": list(image.required_capabilities),
            "metadata": dict(image.metadata),
            "signature": image.signature,
            "boot": dict(image.boot),
        }

    def _image_summary(self, image: AgentImage, metadata: dict[str, Any]) -> dict[str, Any]:
        return {
            "image_id": image.image_id,
            "name": image.name,
            "version": image.version,
            "boot_kind": (image.boot or {}).get("kind", "fresh"),
            "default_tools": list(image.default_tools),
            "default_skills": list(image.default_skills),
            "required_capabilities_count": len(image.required_capabilities),
            **metadata,
        }

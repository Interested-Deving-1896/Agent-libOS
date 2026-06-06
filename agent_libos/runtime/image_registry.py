from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models import AgentImage, Capability, CapabilityRight, EventType
from agent_libos.models.exceptions import CapabilityDenied, ValidationError
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
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
    }

    def __init__(
        self,
        images: dict[str, AgentImage],
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        tool_exists: Any,
        config: AgentLibOSConfig | None = None,
    ):
        self.config = config or DEFAULT_CONFIG
        self.images = images
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        self.tool_exists = tool_exists

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
        )

    def _validate_image(self, image: AgentImage) -> None:
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
            try:
                self.tool_exists(tool_name)
            except Exception as exc:
                raise ValidationError(f"unknown tool in AgentImage default_tools: {tool_name}") from exc
        for spec in image.required_capabilities:
            self._validate_capability_spec(spec)

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

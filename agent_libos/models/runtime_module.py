from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_libos.models.base import StrEnum


class RuntimeModuleStatus(StrEnum):
    LOADED = "loaded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RuntimeModuleRegistration:
    """Typed summary of the runtime surfaces published by one module."""

    tools: tuple[str, ...] = ()
    images: tuple[str, ...] = ()
    syscalls: tuple[str, ...] = ()
    provider_hooks: Mapping[str, int] = field(default_factory=dict)
    startup_hooks: tuple[str, ...] = ()
    durable_object_release_finalizers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", _string_tuple(self.tools, "tools"))
        object.__setattr__(self, "images", _string_tuple(self.images, "images"))
        object.__setattr__(self, "syscalls", _string_tuple(self.syscalls, "syscalls"))
        object.__setattr__(
            self,
            "startup_hooks",
            _string_tuple(self.startup_hooks, "startup_hooks"),
        )
        object.__setattr__(
            self,
            "durable_object_release_finalizers",
            _string_tuple(
                self.durable_object_release_finalizers,
                "durable_object_release_finalizers",
            ),
        )
        if not isinstance(self.provider_hooks, Mapping):
            raise ValueError("runtime module provider_hooks must be a mapping")
        provider_hooks: dict[str, int] = {}
        for kind, count in self.provider_hooks.items():
            if not isinstance(kind, str) or not kind.strip():
                raise ValueError(
                    "runtime module provider hook kind must be a non-empty string"
                )
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(
                    "runtime module provider hook count must be a non-negative integer"
                )
            provider_hooks[kind] = count
        object.__setattr__(self, "provider_hooks", provider_hooks)

    @classmethod
    def from_mapping(
        cls,
        value: RuntimeModuleRegistration | Mapping[str, Any] | None,
    ) -> RuntimeModuleRegistration:
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("runtime module registration must be a mapping")
        return cls(
            tools=_mapping_sequence(value, "tools"),
            images=_mapping_sequence(value, "images"),
            syscalls=_mapping_sequence(value, "syscalls"),
            provider_hooks=_mapping_provider_hooks(value),
            startup_hooks=_mapping_sequence(value, "startup_hooks"),
            durable_object_release_finalizers=_mapping_sequence(
                value,
                "durable_object_release_finalizers",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": list(self.tools),
            "images": list(self.images),
            "syscalls": list(self.syscalls),
            "provider_hooks": dict(self.provider_hooks),
            "startup_hooks": list(self.startup_hooks),
            "durable_object_release_finalizers": list(
                self.durable_object_release_finalizers
            ),
        }


@dataclass(frozen=True, slots=True)
class RuntimeModule:
    """Durable module-publication record returned by storage repositories."""

    module_id: str
    name: str
    version: str
    entrypoint: str
    manifest_path: str
    manifest_sha256: str
    source_path: str
    source_sha256: str
    status: RuntimeModuleStatus
    loaded_at: str | None
    registered: RuntimeModuleRegistration = field(
        default_factory=RuntimeModuleRegistration
    )
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    updated_at: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("module_id", "name", "version", "manifest_path"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"runtime module {field_name} must be a non-empty string"
                )
        try:
            status = RuntimeModuleStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid runtime module status: {self.status!r}") from exc
        object.__setattr__(self, "status", status)
        object.__setattr__(
            self,
            "registered",
            RuntimeModuleRegistration.from_mapping(self.registered),
        )
        if not isinstance(self.metadata, Mapping):
            raise ValueError("runtime module metadata must be a mapping")
        object.__setattr__(self, "metadata", dict(self.metadata))
        if status == RuntimeModuleStatus.LOADED:
            if not self.loaded_at:
                raise ValueError("loaded runtime module requires loaded_at")
            if self.error is not None:
                raise ValueError("loaded runtime module cannot carry an error")
        elif self.loaded_at is not None:
            raise ValueError("failed runtime module cannot carry loaded_at")

    @classmethod
    def from_persisted(cls, value: Mapping[str, Any]) -> RuntimeModule:
        if not isinstance(value, Mapping):
            raise ValueError("persisted runtime module must be a mapping")
        return cls(
            module_id=value["module_id"],
            name=value["name"],
            version=value["version"],
            entrypoint=value["entrypoint"],
            manifest_path=value["manifest_path"],
            manifest_sha256=value["manifest_sha256"],
            source_path=value["source_path"],
            source_sha256=value["source_sha256"],
            status=value["status"],
            loaded_at=value.get("loaded_at"),
            registered=RuntimeModuleRegistration.from_mapping(
                value.get("registered")
            ),
            error=value.get("error"),
            metadata=value.get("metadata") or {},
            updated_at=value.get("updated_at"),
        )

    def to_public_dict(self) -> dict[str, Any]:
        """Preserve the existing CLI/GUI dictionary response contract."""

        return {
            "module_id": self.module_id,
            "name": self.name,
            "version": self.version,
            "entrypoint": self.entrypoint,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "status": self.status.value,
            "loaded_at": self.loaded_at,
            "registered": self.registered.to_dict(),
            "error": self.error,
            "metadata": dict(self.metadata),
            "updated_at": self.updated_at,
        }


def _string_tuple(value: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"runtime module {field_name} must be a sequence")
    selected: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"runtime module {field_name} entries must be non-empty strings"
            )
        selected.append(item)
    if len(selected) != len(set(selected)):
        raise ValueError(f"runtime module {field_name} entries must be unique")
    return tuple(selected)


def _mapping_sequence(value: Mapping[str, Any], key: str) -> tuple[str, ...]:
    selected = value.get(key, ())
    if isinstance(selected, (str, bytes, bytearray)) or not isinstance(
        selected,
        Sequence,
    ):
        raise ValueError(f"runtime module registration {key} must be a sequence")
    return tuple(selected)


def _mapping_provider_hooks(value: Mapping[str, Any]) -> Mapping[str, int]:
    selected = value.get("provider_hooks", {})
    if not isinstance(selected, Mapping):
        raise ValueError("runtime module registration provider_hooks must be a mapping")
    return dict(selected)

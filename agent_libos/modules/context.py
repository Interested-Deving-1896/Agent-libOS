from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from agent_libos.models import AgentImage
from agent_libos.models.exceptions import ValidationError
from agent_libos.modules.schema import ModuleManifest
from agent_libos.tools.base import BaseAgentTool

if TYPE_CHECKING:
    from agent_libos.runtime.syscalls import LibOSSyscallSession
    from agent_libos.runtime.runtime import Runtime

SyscallHandler = Callable[["LibOSSyscallSession", dict[str, Any]], Any]
StartupHook = Callable[["Runtime"], Any]
ProviderHook = Callable[["Runtime"], Any]


@dataclass
class ModuleContext:
    """Buffered registration API exposed to trusted startup modules.

    Calls on this context do not immediately mutate the runtime. The module
    registry validates the declared provides set, preflights collisions, then
    applies the buffered registrations atomically enough for startup use.
    """

    runtime: "Runtime"
    manifest: ModuleManifest
    enforce_provides: bool = True
    tools: list[BaseAgentTool] = field(default_factory=list)
    images: list[AgentImage] = field(default_factory=list)
    syscalls: dict[str, SyscallHandler] = field(default_factory=dict)
    provider_hooks: dict[str, list[ProviderHook]] = field(default_factory=lambda: defaultdict(list))
    startup_hooks: dict[str, StartupHook] = field(default_factory=dict)

    @property
    def module_id(self) -> str:
        return self.manifest.module_id

    @property
    def actor(self) -> str:
        return f"module:{self.module_id}"

    def register_tool(self, tool: BaseAgentTool) -> None:
        spec = tool.spec()
        self._require_declared(spec.name, self.manifest.provides.tools, "tool")
        if any(existing.spec().name == spec.name for existing in self.tools):
            raise ValidationError(f"module registered duplicate tool: {spec.name}")
        self.tools.append(tool)

    def register_image(self, image: AgentImage | dict[str, Any]) -> None:
        candidate = image if isinstance(image, AgentImage) else AgentImage(**dict(image))
        self._require_declared(candidate.image_id, self.manifest.provides.images, "image")
        if any(existing.image_id == candidate.image_id for existing in self.images):
            raise ValidationError(f"module registered duplicate image: {candidate.image_id}")
        self.images.append(candidate)

    def register_syscall(self, name: str, handler: SyscallHandler) -> None:
        normalized = self._normalize_name(name, "syscall")
        self._require_declared(normalized, self.manifest.provides.syscalls, "syscall")
        if normalized in self.syscalls:
            raise ValidationError(f"module registered duplicate syscall: {normalized}")
        if not callable(handler):
            raise ValidationError(f"syscall handler is not callable: {normalized}")
        self.syscalls[normalized] = handler

    def register_provider_hook(self, kind: str, hook: ProviderHook) -> None:
        normalized = self._normalize_name(kind, "provider hook")
        self._require_declared(normalized, self.manifest.provides.provider_hooks, "provider hook")
        if normalized in self.provider_hooks:
            raise ValidationError(f"module registered duplicate provider hook: {normalized}")
        if not callable(hook):
            raise ValidationError(f"provider hook is not callable: {normalized}")
        self.provider_hooks[normalized].append(hook)

    def add_startup_hook(self, hook: StartupHook, *, name: str | None = None) -> None:
        hook_name = self._normalize_name(name or getattr(hook, "__name__", ""), "startup hook")
        self._require_declared(hook_name, self.manifest.provides.startup_hooks, "startup hook")
        if hook_name in self.startup_hooks:
            raise ValidationError(f"module registered duplicate startup hook: {hook_name}")
        if not callable(hook):
            raise ValidationError(f"startup hook is not callable: {hook_name}")
        self.startup_hooks[hook_name] = hook

    def registered_summary(self) -> dict[str, Any]:
        return {
            "tools": [tool.spec().name for tool in self.tools],
            "images": [image.image_id for image in self.images],
            "syscalls": sorted(self.syscalls),
            "provider_hooks": {kind: len(hooks) for kind, hooks in self.provider_hooks.items()},
            "startup_hooks": sorted(self.startup_hooks),
        }

    def _require_declared(self, value: str, declared: list[str], kind: str) -> None:
        if self.enforce_provides and value not in declared:
            raise ValidationError(f"module {self.module_id} registered undeclared {kind}: {value}")

    def _normalize_name(self, value: str, kind: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{kind} name must be a non-empty string")
        return value.strip()

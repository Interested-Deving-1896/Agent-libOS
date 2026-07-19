from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Container
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_libos.models import AgentImage
from agent_libos.models.exceptions import ValidationError
from agent_libos.modules.schema import ModuleManifest
from agent_libos.tools.base import BaseAgentTool

SyscallHandler = Callable[[Any, dict[str, Any]], Any]
StartupHook = Callable[["ModuleHost"], Any]
ProviderHook = Callable[["ModuleHost"], Any]


class ModuleHost(Protocol):
    """Explicit host ports passed to provider and startup hooks."""

    config: Any
    workspace_root: Any
    audit: Any
    events: Any
    shell: Any
    resources: Any
    data_flow: Any
    operations: Any
    memory: Any
    process: Any
    capability: Any
    protected_operations: Any
    store: Any
    substrate: Any

    def register_tool(self, tool: BaseAgentTool, *, ephemeral: bool = False) -> Any: ...

    def register_image(self, image: AgentImage | dict[str, Any], *, source: str | None = None) -> Any: ...

    def register_syscall(self, name: str, handler: SyscallHandler) -> Any: ...

    def register_provider_hook(self, kind: str, hook: ProviderHook) -> None: ...

    def bind_shutdown_finalizer(self, finalizer: Callable[[], Any]) -> None: ...

    def bind_recovery_cleanup(self, cleanup: Callable[[], Any]) -> None: ...

    def require_recovery_cleanup_lease(self) -> None: ...

    def bind_object_release_finalizer(self, finalizer: Callable[..., None]) -> None: ...

    def bind_durable_object_release_finalizer(
        self,
        finalizer_id: str,
        prepare: Callable[..., Any],
        finalize: Callable[..., None],
    ) -> None: ...

    def add_handle_to_process_view(self, pid: str, handle: Any) -> None: ...

    def get_runtime_attribute(self, name: str, default: Any = None) -> Any: ...

    def set_runtime_attribute(self, name: str, value: Any) -> None: ...

    def set_substrate_attribute(self, name: str, value: Any) -> None: ...


@dataclass(frozen=True)
class ModuleRuntimeView:
    """Read-only runtime surface exposed while a module buffers registrations."""

    config: Any

    def __getattr__(self, name: str) -> Any:
        raise ValidationError(f"module entrypoint cannot access runtime.{name} before module preflight")


@dataclass
class ModuleContext:
    """Buffered registration API exposed to trusted startup modules.

    Calls on this context do not immediately mutate the runtime. The module
    registry validates the declared provides set, preflights collisions, then
    applies the buffered registrations atomically enough for startup use.
    """

    runtime: ModuleRuntimeView
    manifest: ModuleManifest
    enforce_provides: bool = True
    tools: list[BaseAgentTool] = field(default_factory=list)
    images: list[AgentImage] = field(default_factory=list)
    syscalls: dict[str, SyscallHandler] = field(default_factory=dict)
    provider_hooks: dict[str, list[ProviderHook]] = field(default_factory=lambda: defaultdict(list))
    startup_hooks: dict[str, StartupHook] = field(default_factory=dict)
    durable_object_release_finalizers: dict[
        str,
        tuple[Callable[..., Any], Callable[..., None]],
    ] = field(default_factory=dict)
    _declared_tools: frozenset[str] = field(init=False, repr=False)
    _declared_images: frozenset[str] = field(init=False, repr=False)
    _declared_syscalls: frozenset[str] = field(init=False, repr=False)
    _declared_provider_hooks: frozenset[str] = field(init=False, repr=False)
    _declared_startup_hooks: frozenset[str] = field(init=False, repr=False)
    _declared_durable_object_release_finalizers: frozenset[str] = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self._declared_tools = frozenset(self.manifest.provides.tools)
        self._declared_images = frozenset(self.manifest.provides.images)
        self._declared_syscalls = frozenset(self.manifest.provides.syscalls)
        self._declared_provider_hooks = frozenset(self.manifest.provides.provider_hooks)
        self._declared_startup_hooks = frozenset(self.manifest.provides.startup_hooks)
        self._declared_durable_object_release_finalizers = frozenset(
            self.manifest.provides.durable_object_release_finalizers
        )

    @property
    def module_id(self) -> str:
        return self.manifest.module_id

    @property
    def actor(self) -> str:
        return f"module:{self.module_id}"

    def register_tool(self, tool: BaseAgentTool) -> None:
        spec = tool.spec()
        self._require_declared(spec.name, self._declared_tools, "tool")
        if any(existing.spec().name == spec.name for existing in self.tools):
            raise ValidationError(f"module registered duplicate tool: {spec.name}")
        self.tools.append(tool)

    def register_image(self, image: AgentImage | dict[str, Any]) -> None:
        candidate = image if isinstance(image, AgentImage) else AgentImage(**dict(image))
        self._require_declared(candidate.image_id, self._declared_images, "image")
        if any(existing.image_id == candidate.image_id for existing in self.images):
            raise ValidationError(f"module registered duplicate image: {candidate.image_id}")
        self.images.append(candidate)

    def register_syscall(self, name: str, handler: SyscallHandler) -> None:
        normalized = self._normalize_name(name, "syscall")
        self._require_declared(normalized, self._declared_syscalls, "syscall")
        if normalized in self.syscalls:
            raise ValidationError(f"module registered duplicate syscall: {normalized}")
        if not callable(handler):
            raise ValidationError(f"syscall handler is not callable: {normalized}")
        self.syscalls[normalized] = handler

    def register_provider_hook(self, kind: str, hook: ProviderHook) -> None:
        normalized = self._normalize_name(kind, "provider hook")
        self._require_declared(normalized, self._declared_provider_hooks, "provider hook")
        if normalized in self.provider_hooks:
            raise ValidationError(f"module registered duplicate provider hook: {normalized}")
        if not callable(hook):
            raise ValidationError(f"provider hook is not callable: {normalized}")
        self.provider_hooks[normalized].append(hook)

    def add_startup_hook(self, hook: StartupHook, *, name: str | None = None) -> None:
        hook_name = self._normalize_name(name or getattr(hook, "__name__", ""), "startup hook")
        self._require_declared(hook_name, self._declared_startup_hooks, "startup hook")
        if hook_name in self.startup_hooks:
            raise ValidationError(f"module registered duplicate startup hook: {hook_name}")
        if not callable(hook):
            raise ValidationError(f"startup hook is not callable: {hook_name}")
        self.startup_hooks[hook_name] = hook

    def bind_durable_object_release_finalizer(
        self,
        finalizer_id: str,
        prepare: Callable[..., Any],
        finalize: Callable[..., None],
    ) -> None:
        selected_id = self._normalize_name(
            finalizer_id,
            "durable object release finalizer",
        )
        self._require_declared(
            selected_id,
            self._declared_durable_object_release_finalizers,
            "durable object release finalizer",
        )
        if selected_id in self.durable_object_release_finalizers:
            raise ValidationError(
                "module registered duplicate durable object release finalizer: "
                f"{selected_id}"
            )
        if not callable(prepare) or not callable(finalize):
            raise ValidationError(
                "durable object release finalizer callbacks must be callable"
            )
        self.durable_object_release_finalizers[selected_id] = (
            prepare,
            finalize,
        )

    def registered_summary(self) -> dict[str, Any]:
        return {
            "tools": [tool.spec().name for tool in self.tools],
            "images": [image.image_id for image in self.images],
            "syscalls": sorted(self.syscalls),
            "provider_hooks": {kind: len(hooks) for kind, hooks in self.provider_hooks.items()},
            "startup_hooks": sorted(self.startup_hooks),
            "durable_object_release_finalizers": sorted(
                self.durable_object_release_finalizers
            ),
        }

    def _require_declared(self, value: str, declared: Container[str], kind: str) -> None:
        if self.enforce_provides and value not in declared:
            raise ValidationError(f"module {self.module_id} registered undeclared {kind}: {value}")

    def _normalize_name(self, value: str, kind: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{kind} name must be a non-empty string")
        return value.strip()

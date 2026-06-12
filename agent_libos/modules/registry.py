from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, TYPE_CHECKING

from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models import EventType
from agent_libos.models.exceptions import NotFound, ValidationError
from agent_libos.modules.context import ModuleContext, StartupHook
from agent_libos.modules.loader import ModuleLoader
from agent_libos.modules.schema import ModuleManifest, ModuleProvides, ModuleSource
from agent_libos.utils.ids import utc_now

if TYPE_CHECKING:
    from agent_libos.runtime.runtime import Runtime


class RuntimeModuleRegistry:
    """Startup module registry bound to one Runtime instance."""

    def __init__(self, runtime: "Runtime", *, config: AgentLibOSConfig | None = None) -> None:
        self.runtime = runtime
        self.config = config or DEFAULT_CONFIG
        self._provider_hooks: list[tuple[str, str, StartupHook]] = []
        self._startup_hooks: list[tuple[str, str, StartupHook]] = []
        self._loaded_modules: dict[str, dict[str, Any]] = {}

    def load_core_module(self) -> dict[str, Any]:
        from agent_libos.modules.core import register_module

        manifest = ModuleManifest(
            schema_version=self.config.modules.schema_version,
            module_id="agent-libos-core:v0",
            name="Agent libOS core",
            version="v0",
            entrypoint="agent_libos.modules.core:register_module",
            provides=ModuleProvides(),
            sha256="0" * 64,
            metadata={"internal": True},
        )
        source = ModuleSource(
            manifest=manifest,
            manifest_path="<internal>",
            manifest_sha256="0" * 64,
            source_path="agent_libos.modules.core",
            source_sha256="0" * 64,
            entrypoint_object="register_module",
        )
        return self._load_from_entrypoint(source, register_module, enforce_provides=False)

    def load_startup_modules(
        self,
        manifests: list[str | Path] | tuple[str | Path, ...] | None = None,
        *,
        trusted_modules: list[str] | tuple[str, ...] | None = None,
        trusted_sha256: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        selected = list(self.config.modules.manifest_paths)
        selected.extend(str(item) for item in (manifests or ()))
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for manifest_path in selected:
            resolved = str(Path(manifest_path).expanduser().resolve())
            if resolved in seen:
                raise ValidationError(f"duplicate startup module manifest: {resolved}")
            seen.add(resolved)
            results.append(
                self.load_module_manifest(
                    resolved,
                    trusted_modules=trusted_modules,
                    trusted_sha256=trusted_sha256,
                )
            )
        return results

    def load_module_manifest(
        self,
        manifest_path: str | Path,
        *,
        trusted_modules: list[str] | tuple[str, ...] | None = None,
        trusted_sha256: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        loader = ModuleLoader(
            self.config,
            trusted_modules=tuple(trusted_modules or ()),
            trusted_sha256=tuple(trusted_sha256 or ()),
        )
        try:
            source, entrypoint = loader.load(manifest_path)
            return self._load_from_entrypoint(source, entrypoint, enforce_provides=True)
        except Exception as exc:
            self._record_failed_manifest(manifest_path, exc)
            if self.config.modules.load_policy == "warn":
                return {"status": "failed", "manifest_path": str(manifest_path), "error": str(exc)}
            raise

    def verify_manifest(
        self,
        manifest_path: str | Path,
        *,
        trusted_modules: list[str] | tuple[str, ...] | None = None,
        trusted_sha256: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        loader = ModuleLoader(
            self.config,
            trusted_modules=tuple(trusted_modules or ()),
            trusted_sha256=tuple(trusted_sha256 or ()),
        )
        return loader.verify(manifest_path)

    def list_modules(self, limit: int | None = None) -> list[dict[str, Any]]:
        selected_limit = self.config.modules.discover_limit if limit is None else limit
        return self.runtime.store.list_runtime_modules(limit=selected_limit)

    def inspect_module(self, module_id: str) -> dict[str, Any]:
        found = self.runtime.store.get_runtime_module(module_id)
        if found is None:
            raise NotFound(f"runtime module not found: {module_id}")
        return found

    def is_loaded(self, module_id: str, source_sha256: str | None = None) -> bool:
        found = self._loaded_modules.get(module_id)
        if found is None:
            return False
        return source_sha256 is None or found.get("source_sha256") == source_sha256

    def loaded_module_summaries(self) -> list[dict[str, Any]]:
        return [dict(item) for item in sorted(self._loaded_modules.values(), key=lambda value: value["module_id"])]

    def run_startup_hooks(self) -> None:
        for module_id, hook_name, hook in list(self._provider_hooks):
            result = hook(self.runtime)
            if inspect.isawaitable(result):
                raise ValidationError(f"provider hook must be synchronous before runtime start: {module_id}:{hook_name}")
            self.runtime.audit.record(
                actor=f"module:{module_id}",
                action="module.provider_hook",
                target=f"module:{module_id}:{hook_name}",
                decision={"hook": hook_name},
            )
        for module_id, hook_name, hook in list(self._startup_hooks):
            result = hook(self.runtime)
            if inspect.isawaitable(result):
                raise ValidationError(f"startup hook must be synchronous before runtime start: {module_id}:{hook_name}")
            self.runtime.audit.record(
                actor=f"module:{module_id}",
                action="module.startup_hook",
                target=f"module:{module_id}:{hook_name}",
                decision={"hook": hook_name},
            )

    def _load_from_entrypoint(self, source: ModuleSource, entrypoint: Any, *, enforce_provides: bool) -> dict[str, Any]:
        ctx = ModuleContext(self.runtime, source.manifest, enforce_provides=enforce_provides)
        result = entrypoint(ctx)
        if inspect.isawaitable(result):
            raise ValidationError(f"module entrypoint must be synchronous: {source.manifest.module_id}")
        self._preflight_context(ctx)
        self._apply_context(ctx)
        now = utc_now()
        summary = ctx.registered_summary()
        self.runtime.store.upsert_runtime_module(
            module_id=source.manifest.module_id,
            name=source.manifest.name,
            version=source.manifest.version,
            entrypoint=source.manifest.entrypoint,
            manifest_path=source.manifest_path,
            manifest_sha256=source.manifest_sha256,
            source_path=source.source_path,
            source_sha256=source.source_sha256,
            status="loaded",
            loaded_at=now,
            registered=summary,
            error=None,
            metadata=source.manifest.metadata,
        )
        self._loaded_modules[source.manifest.module_id] = {
            "module_id": source.manifest.module_id,
            "name": source.manifest.name,
            "version": source.manifest.version,
            "manifest_sha256": source.manifest_sha256,
            "source_sha256": source.source_sha256,
            "entrypoint": source.manifest.entrypoint,
            "registered": summary,
        }
        self.runtime.events.emit(
            EventType.MODULE_LOADED,
            source="runtime",
            target=f"module:{source.manifest.module_id}",
            payload={"module_id": source.manifest.module_id, "registered": summary},
        )
        self.runtime.audit.record(
            actor="runtime",
            action="module.load",
            target=f"module:{source.manifest.module_id}",
            decision={
                "module_id": source.manifest.module_id,
                "version": source.manifest.version,
                "manifest_sha256": source.manifest_sha256,
                "source_sha256": source.source_sha256,
                "registered": summary,
            },
        )
        return self.runtime.store.get_runtime_module(source.manifest.module_id) or {}

    def _preflight_context(self, ctx: ModuleContext) -> None:
        pending_tools = {tool.spec().name for tool in ctx.tools}
        for tool_name in pending_tools:
            try:
                self.runtime.tools.resolve(tool_name)
            except NotFound:
                pass
            else:
                raise ValidationError(f"module tool already exists: {tool_name}")
        for image in ctx.images:
            if image.image_id in self.runtime.images:
                raise ValidationError(f"module image already exists: {image.image_id}")
            original_tool_exists = self.runtime.image_registry.tool_exists
            self.runtime.image_registry.tool_exists = lambda tool_name: (
                self.runtime.tools.resolve(tool_name) if tool_name not in pending_tools else tool_name
            )
            try:
                self.runtime.image_registry._validate_image(image)
            finally:
                self.runtime.image_registry.tool_exists = original_tool_exists
            for tool_name in image.default_tools:
                if tool_name in pending_tools:
                    continue
                try:
                    self.runtime.tools.resolve(tool_name)
                except NotFound as exc:
                    raise ValidationError(f"module image references unknown tool: {tool_name}") from exc
        for syscall in ctx.syscalls:
            if self.runtime.syscalls.get(syscall) is not None or syscall in self.runtime.syscalls.reserved_names:
                raise ValidationError(f"module syscall already exists or is built-in: {syscall}")

    def _apply_context(self, ctx: ModuleContext) -> None:
        for tool in ctx.tools:
            handle = self.runtime.tools.register_tool(tool, registered_by=ctx.actor, scope=f"module:{ctx.module_id}")
            self.runtime.audit.record(
                actor=ctx.actor,
                action="module.register_tool",
                target=f"tool:{handle.tool_id}",
                decision={"module_id": ctx.module_id, "tool": handle.name},
            )
        for image in ctx.images:
            result = self.runtime.image_registry.register(image, actor=ctx.actor, replace=False, require_capability=False)
            self.runtime.audit.record(
                actor=ctx.actor,
                action="module.register_image",
                target=f"image:{result.image.image_id}",
                decision={"module_id": ctx.module_id, "image_id": result.image.image_id},
            )
        for name, handler in ctx.syscalls.items():
            self.runtime.syscalls.register(name, handler, registered_by=ctx.actor)
            self.runtime.audit.record(
                actor=ctx.actor,
                action="module.register_syscall",
                target=f"syscall:{name}",
                decision={"module_id": ctx.module_id, "syscall": name},
            )
        for kind, hooks in ctx.provider_hooks.items():
            self.runtime.provider_hooks.setdefault(kind, []).extend(hooks)
            for index, hook in enumerate(hooks):
                self._provider_hooks.append((ctx.module_id, f"{kind}:{index}", hook))
            self.runtime.audit.record(
                actor=ctx.actor,
                action="module.register_provider_hook",
                target=f"provider_hook:{kind}",
                decision={"module_id": ctx.module_id, "kind": kind, "count": len(hooks)},
            )
        for name, hook in ctx.startup_hooks.items():
            self._startup_hooks.append((ctx.module_id, name, hook))

    def _record_failed_manifest(self, manifest_path: str | Path, exc: Exception) -> None:
        path = str(manifest_path)
        module_id = f"failed:{Path(path).name}"
        try:
            verification = ModuleLoader(self.config).verify(manifest_path)
            module_id = verification["module_id"]
            name = verification["name"]
            version = verification["version"]
            entrypoint = verification["entrypoint"]
            manifest_sha256 = verification["manifest_sha256"]
            source_path = verification["source_path"]
            source_sha256 = verification["source_sha256"]
            metadata = {"verification": verification}
        except Exception:
            name = module_id
            version = "unknown"
            entrypoint = ""
            manifest_sha256 = ""
            source_path = ""
            source_sha256 = ""
            metadata = {}
        self.runtime.store.upsert_runtime_module(
            module_id=module_id,
            name=name,
            version=version,
            entrypoint=entrypoint,
            manifest_path=path,
            manifest_sha256=manifest_sha256,
            source_path=source_path,
            source_sha256=source_sha256,
            status="failed",
            loaded_at=None,
            registered={},
            error=str(exc),
            metadata=metadata,
        )
        self.runtime.audit.record(
            actor="runtime",
            action="module.load_failed",
            target=f"module:{module_id}",
            decision={"manifest_path": path, "error": str(exc), "error_type": type(exc).__name__},
        )

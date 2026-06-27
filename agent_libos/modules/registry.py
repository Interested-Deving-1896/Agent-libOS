from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, TYPE_CHECKING

from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models import EventType
from agent_libos.models.exceptions import CapabilityDenied, NotFound, ValidationError
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
        self._applied_contexts: dict[str, ModuleContext] = {}
        self._applied_sources: dict[str, ModuleSource] = {}
        self._startup_hooks_ran = False

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
            source = loader.resolve(manifest_path)
            if not loader.is_trusted(source.manifest.module_id, source.source_sha256):
                raise CapabilityDenied(
                    "startup module is not trusted: "
                    f"{source.manifest.module_id}:{source.source_sha256}"
                )
            self._require_module_id_available(source.manifest.module_id, source.source_sha256)
            entrypoint = loader.import_entrypoint(source)
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
        if self._startup_hooks_ran:
            return
        for module_id, hook_name, hook in list(self._provider_hooks):
            try:
                self._run_module_hook(module_id, hook_name, hook, kind="provider")
            except Exception as exc:
                self._rollback_external_modules_after_hook_failure(module_id, exc, hook_name=hook_name)
                raise
        for module_id, hook_name, hook in list(self._startup_hooks):
            try:
                self._run_module_hook(module_id, hook_name, hook, kind="startup")
            except Exception as exc:
                self._rollback_external_modules_after_hook_failure(module_id, exc, hook_name=hook_name)
                raise
        self._startup_hooks_ran = True

    def _load_from_entrypoint(self, source: ModuleSource, entrypoint: Any, *, enforce_provides: bool) -> dict[str, Any]:
        ctx = ModuleContext(self.runtime, source.manifest, enforce_provides=enforce_provides)
        try:
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
                "source_kind": source.source_kind,
                "source_root": source.source_root,
                "source_files": [
                    {
                        "path": item.path,
                        "module_path": item.module_path,
                        "size_bytes": item.size_bytes,
                        "sha256": item.sha256,
                    }
                    for item in source.source_files
                ],
                "entrypoint": source.manifest.entrypoint,
                "registered": summary,
            }
            self._applied_contexts[source.manifest.module_id] = ctx
            self._applied_sources[source.manifest.module_id] = source
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
                    "source_kind": source.source_kind,
                    "registered": summary,
                },
            )
            if self._startup_hooks_ran and not self._is_internal_module(source.manifest.module_id):
                self._run_context_hooks(ctx)
            return self.runtime.store.get_runtime_module(source.manifest.module_id) or {}
        except Exception:
            self._rollback_context(ctx)
            self._loaded_modules.pop(source.manifest.module_id, None)
            self._applied_contexts.pop(source.manifest.module_id, None)
            self._applied_sources.pop(source.manifest.module_id, None)
            raise

    def _require_module_id_available(self, module_id: str, source_sha256: str) -> None:
        loaded = self._loaded_modules.get(module_id)
        if loaded is None:
            return
        loaded_sha = loaded.get("source_sha256")
        if loaded_sha == source_sha256:
            raise ValidationError(f"startup module already loaded: {module_id}:{source_sha256}")
        raise ValidationError(
            "startup module id already loaded with a different source hash: "
            f"{module_id}: loaded={loaded_sha}, requested={source_sha256}"
        )

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

    def _run_context_hooks(self, ctx: ModuleContext) -> None:
        for kind, hooks in ctx.provider_hooks.items():
            for index, hook in enumerate(hooks):
                self._run_module_hook(ctx.module_id, f"{kind}:{index}", hook, kind="provider")
        for name, hook in ctx.startup_hooks.items():
            self._run_module_hook(ctx.module_id, name, hook, kind="startup")

    def _run_module_hook(self, module_id: str, hook_name: str, hook: StartupHook, *, kind: str) -> None:
        result = hook(self.runtime)
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise ValidationError(f"{kind} hook must be synchronous: {module_id}:{hook_name}")
        self.runtime.audit.record(
            actor=f"module:{module_id}",
            action=f"module.{kind}_hook",
            target=f"module:{module_id}:{hook_name}",
            decision={"hook": hook_name},
        )

    def _rollback_external_modules_after_hook_failure(self, failed_module_id: str, exc: Exception, *, hook_name: str) -> None:
        module_ids = [
            module_id
            for module_id in reversed(list(self._applied_contexts))
            if not self._is_internal_module(module_id)
        ]
        for module_id in module_ids:
            source = self._applied_sources.get(module_id)
            ctx = self._applied_contexts.get(module_id)
            registered = ctx.registered_summary() if ctx is not None else {}
            self._rollback_module(module_id)
            if source is None:
                continue
            if module_id == failed_module_id:
                failure = exc
            else:
                failure = ValidationError(
                    f"startup module rolled back because {failed_module_id}:{hook_name} failed"
                )
            self._record_failed_source(source, failure, registered=registered)

    def _rollback_module(self, module_id: str) -> None:
        ctx = self._applied_contexts.get(module_id)
        if ctx is not None:
            self._rollback_context(ctx)
        self._loaded_modules.pop(module_id, None)
        self._applied_contexts.pop(module_id, None)
        self._applied_sources.pop(module_id, None)

    def _rollback_context(self, ctx: ModuleContext) -> None:
        self._provider_hooks = [item for item in self._provider_hooks if item[0] != ctx.module_id]
        self._startup_hooks = [item for item in self._startup_hooks if item[0] != ctx.module_id]
        for kind, hooks in list(ctx.provider_hooks.items()):
            hook_ids = {id(hook) for hook in hooks}
            remaining = [hook for hook in self.runtime.provider_hooks.get(kind, []) if id(hook) not in hook_ids]
            if remaining:
                self.runtime.provider_hooks[kind] = remaining
            else:
                self.runtime.provider_hooks.pop(kind, None)
        for name in reversed(list(ctx.syscalls)):
            self.runtime.syscalls.unregister(name, registered_by=ctx.actor)
        for image in reversed(ctx.images):
            stored = self.runtime.store.get_image(image.image_id)
            registered_by = stored[1].get("registered_by") if stored is not None else None
            if registered_by == ctx.actor or (stored is None and self.runtime.images.get(image.image_id) == image):
                self.runtime.images.pop(image.image_id, None)
                self.runtime.store.delete_image(image.image_id, registered_by=ctx.actor)
        for tool in reversed(ctx.tools):
            self.runtime.tools.unregister_tool(tool.spec().name, registered_by=ctx.actor)
        self.runtime.audit.record(
            actor="runtime",
            action="module.rollback",
            target=f"module:{ctx.module_id}",
            decision={"registered": ctx.registered_summary()},
        )

    def _is_internal_module(self, module_id: str) -> bool:
        return module_id == "agent-libos-core:v0"

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
        if module_id in self._loaded_modules:
            self.runtime.audit.record(
                actor="runtime",
                action="module.load_failed",
                target=f"module:{module_id}",
                decision={
                    "manifest_path": path,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "preserved_loaded": True,
                },
            )
            return
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

    def _record_failed_source(
        self,
        source: ModuleSource,
        exc: Exception,
        *,
        registered: dict[str, Any] | None = None,
    ) -> None:
        self.runtime.store.upsert_runtime_module(
            module_id=source.manifest.module_id,
            name=source.manifest.name,
            version=source.manifest.version,
            entrypoint=source.manifest.entrypoint,
            manifest_path=source.manifest_path,
            manifest_sha256=source.manifest_sha256,
            source_path=source.source_path,
            source_sha256=source.source_sha256,
            status="failed",
            loaded_at=None,
            registered=registered or {},
            error=str(exc),
            metadata=source.manifest.metadata,
        )
        self.runtime.audit.record(
            actor="runtime",
            action="module.load_failed",
            target=f"module:{source.manifest.module_id}",
            decision={
                "manifest_path": source.manifest_path,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )

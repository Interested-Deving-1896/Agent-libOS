from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.primitives import ClockPrimitive, FilesystemAdapter, JsonRpcPrimitive, ShellAdapter
from agent_libos.human.manager import HumanObjectManager
from agent_libos.llm.client import LLMClient
from agent_libos.llm.executor import LLMProcessExecutor
from agent_libos.memory.object_memory import ObjectMemoryManager
from agent_libos.models import AgentImage, EventType
from agent_libos.models.exceptions import NotFound
from agent_libos.modules import RuntimeModuleRegistry
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.checkpoint_manager import CheckpointManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.runtime.image_registry import ImageRegistryPrimitive
from agent_libos.runtime.message_manager import ProcessMessageManager
from agent_libos.runtime.process_manager import ProcessManager
from agent_libos.runtime.scheduler import SimpleScheduler
from agent_libos.runtime.syscall_router import SyscallRouter
from agent_libos.runtime.syscalls import BUILTIN_SYSCALL_NAMES
from agent_libos.skills.manager import SkillManager
from agent_libos.storage import SQLiteStore
from agent_libos.substrate import HttpJsonRpcProvider, LocalResourceProviderSubstrate, ResourceProviderSubstrate
from agent_libos.tools.broker import ToolBroker
from agent_libos.utils.ids import utc_now

_RUNTIME_DEFAULTS = DEFAULT_CONFIG.runtime


class Runtime:
    """Composition root for Agent libOS.

    Runtime wires storage, capability checks, primitives, providers, process
    scheduling, ToolBroker, Skills, checkpoints, audit, and LLM execution. Host
    effects should enter through primitives and provider interfaces, not through
    model-facing tools.
    """

    def __init__(
        self,
        store: SQLiteStore,
        llm_client: LLMClient | None = None,
        substrate: ResourceProviderSubstrate | None = None,
        config: AgentLibOSConfig | None = None,
        startup_module_manifests: list[str | Path] | tuple[str | Path, ...] | None = None,
        trusted_modules: list[str] | tuple[str, ...] | None = None,
        trusted_module_sha256: list[str] | tuple[str, ...] | None = None,
    ):
        self.config = config or DEFAULT_CONFIG
        self.substrate = substrate or LocalResourceProviderSubstrate(
            Path.cwd().resolve(),
            namespace=self.config.runtime.workspace_namespace,
        )
        self.workspace_root = Path(getattr(self.substrate, "workspace_root", self.substrate.workspace_display))
        self.store = store
        self.audit = AuditManager(store)
        self.events = EventBus(store)
        self.syscalls = SyscallRouter(self.audit, reserved_names=BUILTIN_SYSCALL_NAMES)
        self.provider_hooks: dict[str, list[Any]] = {}
        self.capability = CapabilityManager(store, self.audit, self.events, config=self.config)
        self.memory = ObjectMemoryManager(store, self.capability, self.audit, self.events, config=self.config)
        self.human = HumanObjectManager(
            store,
            self.capability,
            self.audit,
            self.events,
            provider=self.substrate.human,
            config=self.config,
        )
        self.messages = ProcessMessageManager(store, self.audit, self.events)
        self.human.bind_messages(self.messages)
        self.clock = ClockPrimitive(
            self.audit,
            self.events,
            max_sleep_seconds=self.config.tools.max_sleep_seconds,
            provider=self.substrate.clock,
        )
        self.filesystem = FilesystemAdapter(
            self.capability,
            self.audit,
            self.events,
            human=self.human,
            provider=self.substrate.filesystem,
        )
        self.shell = ShellAdapter(
            self.capability,
            self.audit,
            self.events,
            human=self.human,
            provider=self.substrate.shell,
            config=self.config,
        )
        self.jsonrpc = JsonRpcPrimitive(
            store,
            self.capability,
            self.audit,
            self.events,
            human=self.human,
            provider=getattr(self.substrate, "jsonrpc", HttpJsonRpcProvider()),
            config=self.config,
        )
        self.tools = ToolBroker(
            store,
            self.memory,
            self.capability,
            self.human,
            self.audit,
            self.events,
            workspace_root=self.workspace_root,
            config=self.config,
        )
        self.tools.runtime = self
        self.process = ProcessManager(store, self.memory, self.capability, self.audit, self.events, config=self.config)
        self.process.add_after_spawn_hook(self._configure_process_tools_and_capabilities)
        self.scheduler = SimpleScheduler(store, self.audit, poll_interval_s=self.config.scheduler.poll_interval_s)
        self.checkpoint = CheckpointManager(store, self.audit, self.events, self.capability, config=self.config)
        self.checkpoint.bind_runtime(self)
        self.skills = SkillManager(
            store,
            self.capability,
            self.audit,
            self.events,
            human=self.human,
            config=self.config,
        )
        self.skills.bind_runtime(self)
        self.images: dict[str, AgentImage] = {}
        self.image_registry = ImageRegistryPrimitive(
            self.images,
            self.capability,
            self.audit,
            self.events,
            self.tools.resolve,
            config=self.config,
        )
        self.llm = LLMProcessExecutor(self, llm_client, config=self.config)
        self._current_human_auto_approve: bool | None = None
        self._current_human_auto_policy: str | None = None
        self._current_human_auto_answer: str | None = None
        self.modules = RuntimeModuleRegistry(self, config=self.config)
        self.modules.load_core_module()
        self.modules.load_startup_modules(
            startup_module_manifests,
            trusted_modules=trusted_modules,
            trusted_sha256=trusted_module_sha256,
        )
        self.modules.run_startup_hooks()
        self._closed = False
        self._shutdown_reason: str | None = None

    @classmethod
    def open(
        cls,
        target: str | Path = _RUNTIME_DEFAULTS.local_store_target,
        substrate: ResourceProviderSubstrate | None = None,
        config: AgentLibOSConfig | None = None,
        module_manifests: list[str | Path] | tuple[str | Path, ...] | None = None,
        trusted_modules: list[str] | tuple[str, ...] | None = None,
        trusted_module_sha256: list[str] | tuple[str, ...] | None = None,
    ) -> "Runtime":
        selected_config = config or DEFAULT_CONFIG
        store_target = ":memory:" if str(target) == selected_config.runtime.local_store_target else str(target)
        store = SQLiteStore(store_target)
        try:
            return cls(
                store,
                substrate=substrate,
                config=selected_config,
                startup_module_manifests=module_manifests,
                trusted_modules=trusted_modules,
                trusted_module_sha256=trusted_module_sha256,
            )
        except Exception:
            store.close()
            raise

    def shutdown(self, *, actor: str = "runtime", reason: str = "runtime.shutdown") -> dict[str, Any]:
        """Shut down this host Runtime instance.

        Shutdown is a host lifecycle operation. It stops accepting further use
        of this composition root and releases owned handles, but it does not
        change AgentProcess lifecycle state. A process must still exit through
        the process primitive/tool path, which keeps process authority and audit
        semantics separate from host resource cleanup.
        """
        if self._closed:
            return {"ok": True, "already_shutdown": True, "reason": self._shutdown_reason}
        self._shutdown_reason = reason
        errors: list[dict[str, str]] = []
        self.audit.record(
            actor=actor,
            action="runtime.shutdown",
            target="runtime",
            decision={"reason": reason},
        )
        self.events.emit(
            EventType.RUNTIME_SHUTDOWN,
            source=actor,
            target="runtime",
            payload={"reason": reason},
        )
        for name, component in [
            ("llm.client", getattr(self.llm, "client", None)),
            ("substrate", self.substrate),
        ]:
            try:
                self._shutdown_component(component)
            except Exception as exc:
                errors.append({"component": name, "error": str(exc), "error_type": type(exc).__name__})
        self._closed = True
        self.store.close()
        if errors:
            raise RuntimeError(f"runtime shutdown completed with component errors: {errors}")
        return {"ok": True, "already_shutdown": False, "reason": reason}

    def close(self) -> None:
        """Compatibility alias for shutdown(); prefer Runtime.shutdown()."""
        self.shutdown(actor="runtime.close", reason="runtime.close")

    def _shutdown_component(self, component: Any) -> None:
        if component is None:
            return
        shutdown = getattr(component, "shutdown", None)
        if callable(shutdown):
            shutdown()
            return
        close = getattr(component, "close", None)
        if callable(close):
            close()

    def run_process_once(self, pid: str) -> dict[str, Any]:
        return self.llm.run_once(pid)

    async def arun_process_once(self, pid: str) -> dict[str, Any]:
        return await self.llm.arun_once(pid)

    def run_next_process_once(self) -> Any:
        return self.scheduler.run_once(self.arun_process_once)

    async def arun_next_process_once(self) -> Any:
        return await self.scheduler.arun_once(self.arun_process_once)

    def run_until_idle(
        self,
        max_quanta: int | None = None,
        *,
        process_human_queue: bool = True,
        human: str | None = None,
        human_auto_approve: bool | None = None,
        human_auto_policy: str | None = None,
        human_auto_answer: str | None = None,
    ) -> list[Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.arun_until_idle(
                    max_quanta=max_quanta,
                    process_human_queue=process_human_queue,
                    human=human,
                    human_auto_approve=human_auto_approve,
                    human_auto_policy=human_auto_policy,
                    human_auto_answer=human_auto_answer,
                )
            )
        raise RuntimeError("Cannot call run_until_idle() inside a running event loop. Use await arun_until_idle(...).")

    async def arun_until_idle(
        self,
        max_quanta: int | None = None,
        *,
        process_human_queue: bool = True,
        human: str | None = None,
        human_auto_approve: bool | None = None,
        human_auto_policy: str | None = None,
        human_auto_answer: str | None = None,
    ) -> list[Any]:
        results: list[Any] = []
        remaining = max_quanta if max_quanta is not None else self.config.runtime.run_until_idle_max_quanta
        selected_human = human or self.config.runtime.default_human
        previous_human_context = (
            self._current_human_auto_approve,
            self._current_human_auto_policy,
            self._current_human_auto_answer,
        )
        self._current_human_auto_approve = human_auto_approve
        self._current_human_auto_policy = human_auto_policy
        self._current_human_auto_answer = human_auto_answer
        try:
            while remaining > 0:
                # Run all currently runnable processes first. Human queue work below
                # may wake a process, so this loop intentionally alternates between
                # process execution and terminal queue draining.
                batch = await self.scheduler.arun_until_idle(self.arun_process_once, max_quanta=remaining)
                results.extend(batch)
                remaining -= len(batch)
                if not process_human_queue:
                    break
                processed = await self.human.adrain_terminal_queue(
                    human=selected_human,
                    auto_approve=human_auto_approve,
                    auto_policy=human_auto_policy,
                    auto_answer=human_auto_answer,
                )
                if not processed:
                    break
                self.audit.record(
                    actor="runtime",
                    action="runtime.human_queue_drained",
                    target=f"human:{selected_human}",
                    decision={"request_ids": [request.request_id for request in processed]},
                )
                await asyncio.sleep(0)
        finally:
            (
                self._current_human_auto_approve,
                self._current_human_auto_policy,
                self._current_human_auto_answer,
            ) = previous_human_context
        return results

    def run_process_until_idle(self, pid: str, *, max_quanta: int | None = None) -> list[Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.arun_process_until_idle(pid, max_quanta=max_quanta))
        raise RuntimeError(
            "Cannot call run_process_until_idle() inside a running event loop. "
            "Use await arun_process_until_idle(...)."
        )

    async def arun_process_until_idle(self, pid: str, *, max_quanta: int | None = None) -> list[Any]:
        return await self.scheduler.arun_pid_until_idle(
            pid,
            self.arun_process_once,
            max_quanta=max_quanta or self.config.runtime.run_until_idle_max_quanta,
        )

    def register_image(self, image: AgentImage | dict[str, Any], *, actor: str = "runtime", replace: bool = False) -> None:
        self.image_registry.register(image, actor=actor, replace=replace)

    def get_image(self, image_id: str) -> AgentImage:
        return self.images[image_id]

    def register_skill_from_path(
        self,
        path: str | os.PathLike[str],
        *,
        actor: str = "runtime",
        replace: bool = False,
        source_type: str = "runtime",
    ) -> dict[str, Any]:
        return self.skills.register_skill_from_path(
            path,
            actor=actor,
            replace=replace,
            require_capability=False,
            source_type=source_type,
        )

    def discover_skills(self, text: str | None = None) -> list[dict[str, Any]]:
        return self.skills.discover_skills(text, require_capability=False)

    def inspect_skill(self, skill_id: str) -> dict[str, Any]:
        return self.skills.inspect_skill(skill_id, require_capability=False)

    def activate_skill(self, pid: str, skill_id: str) -> dict[str, Any]:
        return self.skills.activate_skill(pid, skill_id, actor=pid, require_capability=False)

    def unload_skill(self, pid: str, skill_id: str) -> dict[str, Any]:
        return self.skills.unload_skill(pid, skill_id, actor=pid, require_capability=False)

    def trust_skill_source(self, *, source_type: str, source: str, package_sha256: str, actor: str = "runtime") -> dict[str, Any]:
        return self.skills.trust_skill_source(
            actor=actor,
            source_type=source_type,
            source=source,
            package_sha256=package_sha256,
            require_capability=False,
        )

    def exec_process(
        self,
        pid: str,
        image: str,
        *,
        args: dict[str, Any] | None = None,
        goal: dict[str, Any] | str | None = None,
        preserve_memory: bool = True,
        preserve_capabilities: bool = False,
    ) -> Any:
        self._require_image(image)
        self.process.exec(
            pid,
            image,
            args=args,
            goal=goal,
            preserve_memory=preserve_memory,
            preserve_capabilities=preserve_capabilities,
        )
        # Exec swaps the process image and tool table, but deliberately does not
        # apply image required_capabilities. Exec may preserve existing
        # capabilities or shrink them; it never grants new external authority.
        self._configure_process_tools_for_image(pid, image, assigned_by=f"process.exec:{image}")
        self._configure_process_skills_for_image(pid, image, assigned_by=f"process.exec:{image}")
        return self.process.get(pid)

    def spawn_child_process(
        self,
        parent: str,
        goal: dict[str, Any] | str,
        *,
        image: str | None = None,
        inherit_capabilities: list[dict[str, Any]] | None = None,
        working_directory: str | None = None,
    ) -> str:
        parent_process = self.process.get(parent)
        selected_image = image or parent_process.image_id
        self._require_image(selected_image)
        selected_cwd = (
            self.resolve_process_working_directory(parent, working_directory)
            if working_directory is not None
            else parent_process.working_directory
        )
        return self.process.spawn_child(
            parent=parent,
            goal=goal,
            image=selected_image,
            inherit_capabilities=inherit_capabilities,
            working_directory=selected_cwd,
        )

    def set_process_working_directory(self, pid: str, path: str) -> Any:
        relative = self.resolve_process_working_directory(pid, path)
        return self.process.set_working_directory(pid, relative)

    def resolve_process_working_directory(self, pid: str, path: str) -> str:
        current_cwd = self.process.working_directory(pid)
        target, relative = self.filesystem.resolve_path(path, cwd=current_cwd)
        state = self.filesystem.provider.state(target)
        if not state.exists:
            raise NotFound(f"working directory does not exist: {relative}")
        if state.kind != "directory":
            raise NotFound(f"working directory is not a directory: {relative}")
        return relative or "."

    def _configure_process_tools_and_capabilities(self, pid: str, image_id: str) -> None:
        process = self.store.get_process(pid)
        image = self.images.get(image_id) or self.images[self.config.runtime.default_image_id]
        # Tool visibility is fixed from the AgentImage at process creation time.
        # External-resource authority is still enforced later by the primitives.
        try:
            self._configure_process_tools_for_image(pid, image.image_id, assigned_by=f"image:{image_id}")
        except Exception as exc:
            self.audit.record(
                actor="runtime",
                action="image.default_tool_configure_failed",
                target=f"process:{pid}",
                decision={"image": image_id, "error": str(exc)},
            )
        try:
            self._configure_process_skills_for_image(pid, image.image_id, assigned_by=f"image:{image_id}")
        except Exception as exc:
            self.audit.record(
                actor="runtime",
                action="image.default_skill_configure_failed",
                target=f"process:{pid}",
                decision={"image": image_id, "error": str(exc)},
            )
        if process is not None:
            self.checkpoint.grant_process_defaults(pid, issued_by=f"image:{image_id}")
        if process is not None and process.parent_pid is not None:
            self.audit.record(
                actor="runtime",
                action="image.default_capability_skipped_for_child",
                target=f"process:{pid}",
                decision={"image": image_id, "parent_pid": process.parent_pid},
            )
            return
        for spec in image.required_capabilities:
            try:
                self.capability.grant(
                    subject=pid,
                    resource=spec["resource"],
                    rights=spec.get("rights", []),
                    issued_by=f"image:{image_id}",
                    constraints=spec.get("constraints"),
                    expires_at=spec.get("expires_at"),
                    delegable=spec.get("delegable", False),
                    revocable=spec.get("revocable", True),
                )
            except Exception as exc:
                self.audit.record(
                    actor="runtime",
                    action="image.default_capability_grant_failed",
                    target=f"process:{pid}",
                    decision={"capability": spec, "error": str(exc)},
                )

    def _require_image(self, image_id: str) -> AgentImage:
        image = self.images.get(image_id)
        if image is None:
            raise NotFound(f"agent image not found: {image_id}")
        return image

    def _configure_process_tools_for_image(self, pid: str, image_id: str, assigned_by: str) -> dict[str, str]:
        image = self._require_image(image_id)
        tool_names = {"process_exit", "create_memory_object", *image.default_tools}
        return self.tools.configure_process_tools(pid, sorted(tool_names), assigned_by=assigned_by)

    def _configure_process_skills_for_image(self, pid: str, image_id: str, assigned_by: str) -> None:
        process = self.store.get_process(pid)
        if process is None:
            return
        self._apply_loaded_skill_tool_table(pid)
        image = self._require_image(image_id)
        for skill_id in image.default_skills:
            process = self.store.get_process(pid)
            if process is not None and skill_id in process.loaded_skills:
                continue
            self.skills.activate_skill(pid, skill_id, actor=assigned_by, require_capability=False)

    def _apply_loaded_skill_tool_table(self, pid: str) -> None:
        process = self.store.get_process(pid)
        if process is None or not process.loaded_skills:
            return
        updated = dict(process.tool_table)
        for loaded in process.loaded_skills.values():
            if not isinstance(loaded, dict):
                continue
            for mapping_key in ["tool_ids", "jit_tool_ids"]:
                mapping = loaded.get(mapping_key)
                if not isinstance(mapping, dict):
                    continue
                for name, tool_id in mapping.items():
                    if isinstance(name, str) and isinstance(tool_id, str):
                        updated[name] = tool_id
        process.tool_table = updated
        process.updated_at = utc_now()
        self.store.update_process(process)

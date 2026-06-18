from __future__ import annotations

import asyncio
import hashlib
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.primitives import ClockPrimitive, FilesystemAdapter, JsonRpcPrimitive, ShellAdapter
from agent_libos.human.manager import HumanObjectManager
from agent_libos.llm.client import LLMClient
from agent_libos.llm.executor import LLMProcessExecutor
from agent_libos.memory.object_memory import ObjectMemoryManager
from agent_libos.models import AgentImage, EventType, ObjectHandle, ProcessStatus, ToolHandle, ToolSpec
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
from agent_libos.utils.ids import new_id, utc_now
from agent_libos.utils.serde import dumps, loads

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
            store=self.store,
            config=self.config,
        )
        self.image_registry.bind_runtime(self)
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
        self.image_registry.load_persisted_images()
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
        remaining = self.config.runtime.run_until_idle_max_quanta if max_quanta is None else max_quanta
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
            while remaining is None or remaining > 0:
                # Run all currently runnable processes first. Human queue work below
                # may wake a process, so this loop intentionally alternates between
                # process execution and terminal queue draining.
                batch = await self.scheduler.arun_until_idle(self.arun_process_once, max_quanta=remaining)
                results.extend(batch)
                if remaining is not None:
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
        selected_quanta = self.config.runtime.run_until_idle_max_quanta if max_quanta is None else max_quanta
        return await self.scheduler.arun_pid_until_idle(
            pid,
            self.arun_process_once,
            max_quanta=selected_quanta,
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
        selected_image = self._require_image(image)
        if selected_image.boot.get("kind", "fresh") == "checkpoint_commit":
            self._instantiate_checkpoint_commit_image(pid, selected_image)
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
        try:
            image = self._require_image(image_id)
        except Exception as exc:
            if process is not None:
                process.status = ProcessStatus.FAILED
                process.status_message = str(exc)
                process.updated_at = utc_now()
                self.store.update_process(process)
            raise
        is_checkpoint_commit = image.boot.get("kind", "fresh") == "checkpoint_commit"
        # Tool visibility is fixed from the AgentImage at process creation time.
        # External-resource authority is still enforced later by the primitives.
        self._configure_process_tools_for_image(pid, image.image_id, assigned_by=f"image:{image_id}")
        if is_checkpoint_commit:
            self._instantiate_checkpoint_commit_image(pid, image)
            process = self.store.get_process(pid)
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
        if is_checkpoint_commit:
            self.audit.record(
                actor="runtime",
                action="image.required_capabilities_declared_only",
                target=f"process:{pid}",
                decision={
                    "image": image_id,
                    "required_capabilities": len(image.required_capabilities),
                    "reason": "checkpoint commit images never grant external authority automatically",
                },
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

    def _instantiate_checkpoint_commit_image(self, pid: str, image: AgentImage) -> None:
        artifact = self._load_image_artifact(image)
        self.checkpoint._require_snapshot_modules({"modules": artifact.get("modules", [])})
        remapped = self._remap_image_artifact_for_process(pid, artifact)
        self._insert_committed_memory_rows(remapped)
        self._restore_committed_registry_rows(artifact)
        tool_table = self._restore_committed_tool_table(pid, artifact)
        process = self.process.get(pid)
        process.working_directory = str(artifact.get("working_directory") or process.working_directory or ".")
        process.loaded_skills = self._remap_loaded_skills(artifact.get("loaded_skills", {}), tool_table)
        process.tool_table = tool_table
        self._merge_committed_memory_view(process, artifact, remapped)
        process.updated_at = utc_now()
        self.store.update_process(process)
        self.audit.record(
            actor=f"image:{image.image_id}",
            action="image.boot.checkpoint_commit",
            target=f"process:{pid}",
            decision={
                "image_id": image.image_id,
                "artifact_id": image.boot.get("artifact_id"),
                "source_checkpoint_id": artifact.get("source_checkpoint_id"),
                "objects": len(remapped["object_payloads"]),
                "tools": sorted(tool_table),
            },
        )

    def _load_image_artifact(self, image: AgentImage) -> dict[str, Any]:
        artifact_id = str(image.boot.get("artifact_id") or "")
        expected_sha256 = str(image.boot.get("artifact_sha256") or "")
        found = self.store.get_image_artifact(artifact_id)
        if found is None:
            raise NotFound(f"image artifact not found: {artifact_id}")
        artifact, metadata = found
        if artifact.get("kind") != "checkpoint_commit":
            raise RuntimeError(f"image artifact has unsupported kind: {artifact.get('kind')}")
        if artifact.get("artifact_version") != self.config.image_commit.artifact_version:
            raise RuntimeError(
                "image artifact version mismatch: "
                f"{artifact.get('artifact_version')} != {self.config.image_commit.artifact_version}"
            )
        actual_sha256 = hashlib.sha256(dumps(artifact).encode("utf-8")).hexdigest()
        if expected_sha256 and metadata.get("sha256") != expected_sha256:
            raise RuntimeError(f"image artifact hash mismatch for {artifact_id}")
        if metadata.get("sha256") != actual_sha256:
            raise RuntimeError(f"image artifact content hash mismatch for {artifact_id}")
        return artifact

    def _remap_image_artifact_for_process(self, pid: str, artifact: dict[str, Any]) -> dict[str, Any]:
        source_pid = str(artifact["source_pid"])
        old_oids = list(artifact.get("object_oids", []))
        oid_map = {oid: new_id("obj") for oid in old_oids}
        namespace_map = {
            namespace: self._remap_image_artifact_namespace(pid, source_pid, namespace)
            for namespace in artifact.get("namespaces", [])
        }
        cap_rows = artifact.get("rows", {}).get("capabilities", [])
        cap_map = {row["cap_id"]: new_id("cap") for row in cap_rows}
        now = utc_now()
        object_rows = [
            self._remap_committed_object_row(row, pid, oid_map, namespace_map, now)
            for row in artifact.get("rows", {}).get("objects", [])
            if row["oid"] in oid_map
        ]
        namespace_rows = [
            self._remap_committed_namespace_row(row, pid, namespace_map, now)
            for row in artifact.get("rows", {}).get("object_namespaces", [])
            if row["namespace"] in namespace_map
        ]
        link_rows = [
            self._remap_committed_link_row(row, oid_map, now)
            for row in artifact.get("rows", {}).get("object_links", [])
            if row["src_oid"] in oid_map and row["dst_oid"] in oid_map
        ]
        capability_rows = [
            self._remap_committed_capability_row(row, pid, oid_map, namespace_map, cap_map, now)
            for row in cap_rows
            if row["subject"] == source_pid
        ]
        payloads = {
            oid_map[oid]: deepcopy(payload)
            for oid, payload in artifact.get("object_payloads", {}).items()
            if oid in oid_map
        }
        return {
            "oid_map": oid_map,
            "namespace_map": namespace_map,
            "capability_map": cap_map,
            "object_namespaces": namespace_rows,
            "objects": object_rows,
            "object_links": link_rows,
            "capabilities": capability_rows,
            "object_payloads": payloads,
        }

    def _remap_image_artifact_namespace(self, pid: str, source_pid: str, namespace: str) -> str:
        source_process_namespace = self.memory.process_namespace(source_pid)
        if namespace == source_process_namespace:
            return self.memory.process_namespace(pid)
        return f"image_commit/{pid}/{namespace}"

    def _remap_committed_namespace_row(
        self,
        row: dict[str, Any],
        pid: str,
        namespace_map: dict[str, str],
        now: str,
    ) -> dict[str, Any]:
        item = dict(row)
        item["namespace"] = namespace_map[item["namespace"]]
        if item.get("parent_namespace") in namespace_map:
            item["parent_namespace"] = namespace_map[item["parent_namespace"]]
        elif item["namespace"] == self.memory.process_namespace(pid):
            item["parent_namespace"] = None
        item["created_by"] = pid
        metadata = loads(item.get("metadata_json"), {})
        if metadata.get("kind") == "process":
            metadata["pid"] = pid
        item["metadata_json"] = dumps(metadata)
        item["updated_at"] = now
        return item

    def _remap_committed_object_row(
        self,
        row: dict[str, Any],
        pid: str,
        oid_map: dict[str, str],
        namespace_map: dict[str, str],
        now: str,
    ) -> dict[str, Any]:
        item = dict(row)
        old_oid = item["oid"]
        item["oid"] = oid_map[old_oid]
        if item.get("name") == old_oid:
            item["name"] = item["oid"]
        item["namespace"] = namespace_map.get(item["namespace"], item["namespace"])
        item["created_by"] = pid
        provenance = loads(item.get("provenance_json"), {})
        provenance["parent_oids"] = [oid_map.get(oid, oid) for oid in provenance.get("parent_oids", [])]
        item["provenance_json"] = dumps(provenance)
        item["payload_json"] = dumps(self.store._memory_payload_marker(present=True))
        item["created_at"] = now
        item["updated_at"] = now
        return item

    def _remap_committed_link_row(self, row: dict[str, Any], oid_map: dict[str, str], now: str) -> dict[str, Any]:
        item = dict(row)
        item["id"] = new_id("link")
        item["src_oid"] = oid_map[item["src_oid"]]
        item["dst_oid"] = oid_map[item["dst_oid"]]
        item["created_at"] = now
        return item

    def _remap_committed_capability_row(
        self,
        row: dict[str, Any],
        pid: str,
        oid_map: dict[str, str],
        namespace_map: dict[str, str],
        cap_map: dict[str, str],
        now: str,
    ) -> dict[str, Any]:
        item = dict(row)
        item["cap_id"] = cap_map[item["cap_id"]]
        item["subject"] = pid
        item["issuer_cap_id"] = cap_map.get(item.get("issuer_cap_id")) if item.get("issuer_cap_id") else None
        item["parent_cap_id"] = cap_map.get(item.get("parent_cap_id")) if item.get("parent_cap_id") else None
        resource = str(item["resource"])
        if resource.startswith("object:"):
            item["resource"] = f"object:{oid_map[resource.split(':', 1)[1]]}"
        elif resource.startswith("object_namespace:"):
            namespace = resource.split(":", 1)[1]
            item["resource"] = f"object_namespace:{namespace_map[namespace]}"
        item["issued_by"] = f"image.commit:{item['issued_by']}"
        item["issued_at"] = now
        return item

    def _insert_committed_memory_rows(self, remapped: dict[str, Any]) -> None:
        with self.store._lock:
            cur = self.store.conn.cursor()
            for row in remapped["object_namespaces"]:
                exists = cur.execute("SELECT 1 FROM object_namespaces WHERE namespace = ?", (row["namespace"],)).fetchone()
                if exists is None:
                    self.checkpoint._insert_row(cur, "object_namespaces", row)
            for row in remapped["objects"]:
                self.checkpoint._insert_row(cur, "objects", row)
                self.store.set_object_payload(row["oid"], deepcopy(remapped["object_payloads"][row["oid"]]))
            for table in ["object_links", "capabilities"]:
                for row in remapped[table]:
                    self.checkpoint._insert_row(cur, table, row)
            self.store.conn.commit()

    def _restore_committed_registry_rows(self, artifact: dict[str, Any]) -> None:
        rows = artifact.get("rows", {})
        with self.store._lock:
            cur = self.store.conn.cursor()
            for row in rows.get("skills", []):
                self.checkpoint._upsert_row(cur, "skills", row, "skill_id")
            for row in rows.get("skill_trust", []):
                self.checkpoint._upsert_row(cur, "skill_trust", row, "trust_id")
            for row in rows.get("jsonrpc_endpoints", []):
                self.checkpoint._upsert_row(cur, "jsonrpc_endpoints", row, "endpoint_id")
            self.store.conn.commit()

    def _restore_committed_tool_table(self, pid: str, artifact: dict[str, Any]) -> dict[str, str]:
        tool_rows = {row["tool_id"]: row for row in artifact.get("rows", {}).get("tools", [])}
        old_to_new: dict[str, str] = {}
        table: dict[str, str] = {}
        jit_sources = artifact.get("jit_sources", {})
        for name, old_tool_id in artifact.get("tool_table", {}).items():
            if old_tool_id in jit_sources:
                row = tool_rows.get(old_tool_id)
                if row is None:
                    raise RuntimeError(f"committed JIT tool row is missing: {old_tool_id}")
                new_tool_id = new_id("tool")
                old_to_new[old_tool_id] = new_tool_id
                spec = ToolSpec(**loads(row["spec_json"], {}))
                handle = ToolHandle(tool_id=new_tool_id, name=row["name"], capability_id=None, scope=row["scope"])
                self.tools._jit_sources[new_tool_id] = jit_sources[old_tool_id]
                self.tools._handles[new_tool_id] = handle
                self.tools._tool_ids_by_name.setdefault(handle.name, new_tool_id)
                self.store.insert_tool(handle, spec, registered_by=f"image.commit:{pid}", created_at=utc_now(), ephemeral=True)
                table[name] = new_tool_id
                continue
            handle = self.tools.resolve(name)
            old_to_new[old_tool_id] = handle.tool_id
            table[name] = handle.tool_id
        artifact["_tool_id_map"] = old_to_new
        return table

    def _remap_loaded_skills(self, loaded_skills: dict[str, Any], tool_table: dict[str, str]) -> dict[str, Any]:
        updated = deepcopy(loaded_skills or {})
        for loaded in updated.values():
            if not isinstance(loaded, dict):
                continue
            for key in ["tool_ids", "jit_tool_ids"]:
                mapping = loaded.get(key)
                if not isinstance(mapping, dict):
                    continue
                loaded[key] = {
                    name: tool_table[name]
                    for name in mapping
                    if name in tool_table
                }
        return updated

    def _merge_committed_memory_view(self, process: Any, artifact: dict[str, Any], remapped: dict[str, Any]) -> None:
        source = loads(artifact.get("source_process", {}).get("memory_view_json"), {})
        if not source:
            return
        existing_roots = list(process.memory_view.roots) if process.memory_view is not None else []
        roots = []
        cap_map = remapped["capability_map"]
        oid_map = remapped["oid_map"]
        for root in source.get("roots", []):
            old_oid = root.get("oid")
            if old_oid not in oid_map:
                continue
            old_cap = root.get("capability_id")
            new_oid = oid_map[old_oid]
            rights = set(root.get("rights", []))
            new_cap = cap_map.get(old_cap)
            if new_cap is None:
                handle = self.capability.handle_for_object(subject=process.pid, oid=new_oid, rights=rights, issued_by="image.commit")
                new_cap = handle.capability_id
            roots.append(ObjectHandle(oid=new_oid, rights=rights, capability_id=new_cap, expires_at=root.get("expires_at")))
        for handle in existing_roots:
            if all(item.oid != handle.oid for item in roots):
                roots.append(handle)
        if process.memory_view is None:
            process.memory_view = self.memory.create_view(process.pid, roots, mode="mutable")
        else:
            process.memory_view.roots = roots

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

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.evidence import reconcile_pending_external_effects
from agent_libos.primitives import (
    ClockPrimitive,
    FilesystemAdapter,
    JsonRpcPrimitive,
    McpPrimitive,
    ShellAdapter,
)
from agent_libos.human.manager import HumanObjectManager
from agent_libos.llm.executor import LLMProcessExecutor
from agent_libos.llm.profiles import LLMProfileRegistry
from agent_libos.memory.object_memory import ObjectMemoryManager
from agent_libos.models import EventType
from agent_libos.modules import RuntimeModuleRegistry
from agent_libos.modules.host import ModuleHookServices, ModuleStateRegistry
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.blocking_work import BlockingWorkSupervisor
from agent_libos.runtime.authority_manifest_manager import AuthorityManifestManager
from agent_libos.runtime.boundary_descriptors import EXPLAIN_BOUNDARY_DESCRIPTORS
from agent_libos.runtime.boundary_installer import install_explain_boundaries
from agent_libos.runtime.checkpoint_manager import CheckpointManager
from agent_libos.runtime.checkpoint_image import CheckpointImageInstaller
from agent_libos.runtime.data_flow_manager import DataFlowManager
from agent_libos.runtime.descriptor_catalog import (
    register_protected_operation_descriptors,
)
from agent_libos.runtime.event_bus import EventBus
from agent_libos.runtime.explain_manager import ExplainManager
from agent_libos.runtime.image_boot import ImageBootService
from agent_libos.runtime.image_artifact import ImageArtifactLoader
from agent_libos.runtime.image_package import ImagePackageInstaller
from agent_libos.runtime.image_registry import ImageRegistryPrimitive
from agent_libos.runtime.lifecycle import RuntimeLifecycle
from agent_libos.runtime.message_manager import ProcessMessageManager
from agent_libos.runtime.object_tasks import ObjectTaskManager
from agent_libos.runtime.operation_manager import OperationManager
from agent_libos.runtime.process_launch import ProcessLaunchService
from agent_libos.runtime.process_manager import ProcessManager
from agent_libos.runtime.ratings import AgentRatingManager
from agent_libos.runtime.resource_manager import ResourceManager
from agent_libos.runtime.scheduler import SimpleScheduler
from agent_libos.runtime.syscall_router import SyscallRouter
from agent_libos.runtime.syscalls import BUILTIN_SYSCALL_NAMES, LibOSSyscallSession
from agent_libos.runtime.snapshots import ProcessExecStateService
from agent_libos.sdk import ProtectedOperationSDK
from agent_libos.skills.manager import SkillManager
from agent_libos.storage import RuntimeStore, UnitOfWork, open_store
from agent_libos.substrate import (
    HttpJsonRpcProvider,
    LocalResourceProviderSubstrate,
    ResourceProviderSubstrate,
    SdkMcpProvider,
)
from agent_libos.tools.broker import ToolBroker
from agent_libos.utils.ids import new_id

if TYPE_CHECKING:
    from agent_libos.llm.client import LLMClient
    from agent_libos.runtime.runtime import Runtime


RuntimeT = TypeVar("RuntimeT", bound="Runtime")


@dataclass(frozen=True, slots=True)
class RuntimeBuilder(Generic[RuntimeT]):
    """Open and assemble one Runtime from explicit host dependencies."""

    runtime_type: type[RuntimeT]
    config: AgentLibOSConfig = DEFAULT_CONFIG
    substrate: ResourceProviderSubstrate | None = None
    module_manifests: tuple[str | Path, ...] | None = None
    trusted_modules: tuple[str, ...] | None = None
    trusted_module_sha256: tuple[str, ...] | None = None

    def open(self, target: str | Path | None = None) -> RuntimeT:
        store = open_store(target, config=self.config)
        try:
            return self.from_store(store)
        except BaseException:
            store.close()
            raise

    def from_store(
        self,
        store: RuntimeStore,
        *,
        llm_client: LLMClient | None = None,
    ) -> RuntimeT:
        return self.runtime_type(
            store,
            llm_client=llm_client,
            substrate=self.substrate,
            config=self.config,
            startup_module_manifests=self.module_manifests,
            trusted_modules=self.trusted_modules,
            trusted_module_sha256=self.trusted_module_sha256,
        )

    @classmethod
    def assemble_existing(
        cls,
        host: Runtime,
        store: RuntimeStore,
        *,
        llm_client: LLMClient | None,
        substrate: ResourceProviderSubstrate | None,
        config: AgentLibOSConfig | None,
        startup_module_manifests: list[str | Path] | tuple[str | Path, ...] | None,
        trusted_modules: list[str] | tuple[str, ...] | None,
        trusted_module_sha256: list[str] | tuple[str, ...] | None,
    ) -> None:
        cls._configure_foundation(host, store, substrate=substrate, config=config)
        try:
            cls._configure_evidence_and_authority(host)
            cls._configure_host_services(host)
            cls._configure_human_and_primitives(host)
            cls._configure_execution_services(host, store)
            cls._configure_tail(
                host,
                store,
                llm_client=llm_client,
                startup_module_manifests=startup_module_manifests,
                trusted_modules=trusted_modules,
                trusted_module_sha256=trusted_module_sha256,
            )
        except BaseException as original:
            cleanup_errors = cls._cleanup_failed_assembly(host)
            if cleanup_errors:
                cleanup_failures = [
                    RuntimeError(
                        f"{item.get('component')}: {item.get('error_type')}: {item.get('error')}"
                    )
                    for item in cleanup_errors
                ]
                raise ExceptionGroup(
                    "runtime assembly and cleanup failed",
                    [original, *cleanup_failures],
                ) from original
            raise

    @staticmethod
    def _configure_foundation(
        host: Runtime,
        store: RuntimeStore,
        *,
        substrate: ResourceProviderSubstrate | None,
        config: AgentLibOSConfig | None,
    ) -> None:
        host.config = config or DEFAULT_CONFIG
        host.substrate = substrate or LocalResourceProviderSubstrate(
            Path.cwd().resolve(),
            namespace=host.config.runtime.workspace_namespace,
        )
        host.workspace_root = Path(
            getattr(
                host.substrate,
                "workspace_root",
                host.substrate.workspace_display,
            )
        )
        host.store = store
        host.instance_id = new_id("runtime")
        host.store.config = host.config
        host.images = {}
        host.module_state = ModuleStateRegistry()
        host._registry_lifecycle_lock = threading.RLock()
        host.blocking_work = BlockingWorkSupervisor(
            max_workers=max(
                host.config.scheduler.max_workers,
                host.config.object_tasks.max_running_global,
            ),
            shutdown_timeout_s=max(
                host.config.scheduler.shutdown_join_timeout_s,
                host.config.object_tasks.shutdown_join_timeout_s,
            ),
        )

    @staticmethod
    def _configure_evidence_and_authority(host: Runtime) -> None:
        host.uow = UnitOfWork(host.store)
        host.operations = OperationManager(host.uow.evidence)
        host.operations.interrupt_stale_running()
        host.audit = AuditManager(host.uow.evidence, host.operations)
        host.events = EventBus(host.uow.evidence, host.operations)
        host.lifecycle = RuntimeLifecycle(
            store=host.store,
            audit=host.audit,
            events=host.events,
            substrate=host.substrate,
            admission_drain_timeout_s=min(
                host.config.scheduler.shutdown_join_timeout_s,
                host.config.object_tasks.shutdown_join_timeout_s,
            ),
        )
        host.lifecycle.begin_recovery()
        host.capability = CapabilityManager(
            host.uow.authority,
            host.audit,
            host.events,
            config=host.config,
            operations=host.operations,
        )

    @staticmethod
    def _configure_host_services(host: Runtime) -> None:
        host.llms = LLMProfileRegistry(host.uow.processes, config=host.config)
        host.ratings = AgentRatingManager(
            host.uow.processes,
            host.audit,
            config=host.config,
        )
        host.resources = ResourceManager(host.store, host.audit, host.events)
        host.syscalls = SyscallRouter(
            host.audit,
            reserved_names=BUILTIN_SYSCALL_NAMES,
        )
        host.provider_hooks = {}
        host.authority_manifests = AuthorityManifestManager(
            host.uow.authority,
            host.capability,
            host.audit,
            host.events,
            host.images,
            config=host.config,
        )
        host.explain = ExplainManager(host.store, host.authority_manifests)
        host.memory = ObjectMemoryManager(
            host.uow,
            host.capability,
            host.audit,
            host.events,
            config=host.config,
            resources=host.resources,
            operations=host.operations,
        )
        host.data_flow = DataFlowManager(
            host.uow.authority,
            host.capability,
            host.audit,
            host.events,
            host.authority_manifests,
            host.uow.objects,
            memory=host.memory,
            config=host.config,
            blocking_work_supervisor=host.blocking_work,
        )
        host.protected_operations = ProtectedOperationSDK(
            effects=host.uow.protected_effects,
            authority_policy=host.authority_manifests,
            capabilities=host.capability,
            audit=host.audit,
            events=host.events,
            resources=host.resources,
            operations=host.operations,
            data_flow=host.data_flow,
        )
        host.external_primitive_boundary_names = (
            register_protected_operation_descriptors(host.protected_operations)
        )
        host.process = ProcessManager(
            host.uow,
            host.memory,
            host.capability,
            host.audit,
            host.events,
            config=host.config,
            resources=host.resources,
            llm_profile_resolver=host._resolve_launch_llm_profile_id,
            authority_manifests=host.authority_manifests,
            data_flow=host.data_flow,
            object_task_terminal_notifier=host._notify_process_terminal,
            owner_instance_id=host.instance_id,
        )
        host.resources.bind_process_kill_finalizer(
            host.process.finalize_killed_processes
        )
        host.messages = ProcessMessageManager(
            host.uow.processes,
            host.audit,
            host.events,
            host.authority_manifests,
            process_manager=host.process,
            config=host.config,
        )

    @staticmethod
    def _configure_human_and_primitives(host: Runtime) -> None:
        host.human = HumanObjectManager(
            host.uow.processes,
            host.uow.authority,
            host.capability,
            host.audit,
            host.events,
            provider=host.substrate.human,
            protected_operations=host.protected_operations,
            authority_policy=host.authority_manifests,
            operations=host.operations,
            requests=host.uow.processes,
            messages=host.messages,
            data_flow=host.data_flow,
            config=host.config,
        )
        host.data_flow.bind_human(host.human)
        host.data_flow.bootstrap_configured_rules()
        host.protected_operations.register_prepared_recovery(
            "human_output_delivery",
            host.human.recover_prepared_output,
        )
        host.clock = ClockPrimitive(
            host.capability,
            host.audit,
            host.events,
            max_sleep_seconds=host.config.tools.max_sleep_seconds,
            provider=host.substrate.clock,
            protected_operations=host.protected_operations,
        )
        host.filesystem = FilesystemAdapter(
            host.capability,
            host.audit,
            host.events,
            protected_operations=host.protected_operations,
            human=host.human,
            provider=host.substrate.filesystem,
            resources=host.resources,
            config=host.config,
        )
        host.shell = ShellAdapter(
            host.capability,
            host.audit,
            host.events,
            protected_operations=host.protected_operations,
            cwd=host.workspace_root,
            human=host.human,
            provider=host.substrate.shell,
            config=host.config,
            resources=host.resources,
        )
        host.jsonrpc = JsonRpcPrimitive(
            host.uow,
            host.capability,
            host.audit,
            host.events,
            protected_operations=host.protected_operations,
            human=host.human,
            provider=getattr(
                host.substrate,
                "jsonrpc",
                HttpJsonRpcProvider(),
            ),
            config=host.config,
            resources=host.resources,
        )
        host.mcp = McpPrimitive(
            host.uow,
            host.capability,
            host.audit,
            host.events,
            protected_operations=host.protected_operations,
            human=host.human,
            provider=getattr(
                host.substrate,
                "mcp",
                SdkMcpProvider(host.workspace_root),
            ),
            config=host.config,
            resources=host.resources,
        )

    @staticmethod
    def _configure_execution_services(host: Runtime, store: RuntimeStore) -> None:
        host.tools = ToolBroker(
            store,
            host.memory,
            host.capability,
            host.human,
            host.audit,
            host.events,
            workspace_root=host.workspace_root,
            config=host.config,
            resources=host.resources,
            unit_of_work=host.uow,
            operations=host.operations,
            data_flow=host.data_flow,
            jit_session_factory=lambda pid: LibOSSyscallSession(
                host,
                pid,
                config=host.config,
            ),
            tool_context_host=host,
            images=host.images,
            registry_lifecycle_lock=host._registry_lifecycle_lock,
            lifecycle=host.lifecycle,
        )
        host.object_tasks = ObjectTaskManager(
            host.uow.processes,
            host.uow.objects,
            host.process,
            host.tools,
            host.memory,
            host.capability,
            host.audit,
            host.events,
            host.operations,
            host.messages,
            host.authority_manifests,
            host.human,
            host.add_handle_to_process_view,
            config=host.config,
            autostart=False,
        )
        host.memory.bind_object_pin_checker(host.object_tasks.has_active_for_owner)
        host.memory.bind_object_change_notifier(
            host.object_tasks.notify_owner_changed
        )
        host.messages.bind_object_tasks(host.object_tasks)
        host.resources.bind_object_task_terminal_notifier(
            host._notify_process_terminal
        )

    @classmethod
    def _configure_tail(
        cls,
        host: Runtime,
        store: RuntimeStore,
        *,
        llm_client: LLMClient | None,
        startup_module_manifests: list[str | Path] | tuple[str | Path, ...] | None,
        trusted_modules: list[str] | tuple[str, ...] | None,
        trusted_module_sha256: list[str] | tuple[str, ...] | None,
    ) -> None:
        host.scheduler = SimpleScheduler(
            host.uow.processes,
            host.audit,
            poll_interval_s=host.config.scheduler.poll_interval_s,
            max_workers=host.config.scheduler.max_workers,
            drain_window_s=host.config.scheduler.drain_window_s,
            shutdown_join_timeout_s=host.config.scheduler.shutdown_join_timeout_s,
            resources=host.resources,
            skip_pid=host.object_tasks.is_runner_pid,
            cancel_process=host.process.cancel,
            blocking_work=host.blocking_work,
            owner_id=host.instance_id,
        )
        host.checkpoint = CheckpointManager(
            store,
            host.audit,
            host.events,
            host.capability,
            scheduler=host.scheduler,
            registry_lifecycle_lock=host._registry_lifecycle_lock,
            memory=host.memory,
            images=host.images,
            authority_manifests=host.authority_manifests,
            tools=host.tools,
            resources=host.resources,
            messages=host.messages,
            config=host.config,
        )
        host.skills = SkillManager(
            host.uow,
            host.capability,
            host.audit,
            host.events,
            host.tools,
            host.filesystem,
            host.process,
            host.images,
            host._registry_lifecycle_lock,
            human=host.human,
            config=host.config,
        )
        cls._configure_image_services(host)
        host.modules = RuntimeModuleRegistry(
            host.uow.extensions,
            host.tools,
            host.images,
            host.image_registry,
            host.syscalls,
            host.provider_hooks,
            host.audit,
            host.events,
            ModuleHookServices.from_host(host),
            host._registry_lifecycle_lock,
            config=host.config,
        )
        host.checkpoint.bind_modules(host.modules)
        cls._configure_image_boot(host)
        host.llm = LLMProcessExecutor(
            unit_of_work=host.uow,
            process=host.process,
            operations=host.operations,
            data_flow=host.data_flow,
            tools=host.tools,
            resources=host.resources,
            llms=host.llms,
            memory=host.memory,
            audit=host.audit,
            events=host.events,
            images=host.images,
            messages=host.messages,
            human=host.human,
            skills=host.skills,
            protected_operations=host.protected_operations,
            authority_manifests=host.authority_manifests,
            capabilities=host.capability,
            client=llm_client,
            config=host.config,
            blocking_work=host.blocking_work,
        )
        host.lifecycle.bind_components(
            scheduler=host.scheduler,
            object_tasks=host.object_tasks,
            modules=host.modules,
            llms=host.llms,
            blocking_work=host.blocking_work,
        )
        cls._load_extensions(
            host,
            startup_module_manifests=startup_module_manifests,
            trusted_modules=trusted_modules,
            trusted_module_sha256=trusted_module_sha256,
        )
        cls._install_operation_boundaries(host)
        with host.lifecycle.recovery_lease():
            host.recovered_prepared_operations = (
                host.protected_operations.recover_prepared()
            )
            host.reconciled_external_effects = reconcile_pending_external_effects(
                host.uow.protected_effects,
                host.substrate,
            )
            host.recovered_resource_usage_reservations = host.resources.recover_usage_reservations()
            host.recovered_exec_publications = host.image_boot.recover_incomplete_publications()
            host.recovered_runtime_publications = host.process.recover_incomplete_publications()
            with host.store.transaction():
                host.recovered_stale_executions = host.uow.processes.recover_stale_executions(
                    owner_id=host.instance_id
                )
                for pid in host.recovered_stale_executions:
                    host.events.emit(
                        EventType.PROCESS_SIGNAL,
                        source="runtime.recovery",
                        target=pid,
                        payload={"pid": pid, "reason": "stale_execution_recovery"},
                    )
                    host.audit.record(
                        actor="runtime.recovery",
                        action="stale_execution_recovery",
                        target=f"process:{pid}",
                        decision={"status": "paused", "owner_instance_id": host.instance_id},
                    )
            host.object_tasks.recover()
        host.lifecycle.begin_starting()
        with host.lifecycle.startup_lease():
            host.modules.run_startup_hooks()
        host.object_tasks.start_worker()
        host.lifecycle.mark_open()

    @staticmethod
    def _configure_image_services(host: Runtime) -> None:
        host.process_exec_state = ProcessExecStateService(
            host.store,
            host.memory,
            host.tools,
        )
        host.image_artifacts = ImageArtifactLoader(
            host.uow.extensions,
            host.config,
        )
        host.checkpoint_image_installer = CheckpointImageInstaller(
            loader=host.image_artifacts,
            store=host.store,
            processes=host.uow.processes,
            memory=host.memory,
            capabilities=host.capability,
            authority_manifests=host.authority_manifests,
            checkpoint=host.checkpoint,
            tools=host.tools,
            audit=host.audit,
        )
        host.image_package_installer = ImagePackageInstaller(
            loader=host.image_artifacts,
            processes=host.uow.processes,
            extensions=host.uow.extensions,
            tools=host.tools,
            filesystem=host.filesystem,
            resources=host.resources,
            audit=host.audit,
            workspace_root=host.workspace_root,
            config=host.config,
        )
        host.image_registry = ImageRegistryPrimitive(
            host.images,
            host.capability,
            host.audit,
            host.events,
            host.tools,
            host.checkpoint,
            host.filesystem,
            host.process.working_directory,
            host._registry_lifecycle_lock,
            store=host.uow.extensions,
            config=host.config,
        )
        host.checkpoint.bind_image_registry(host.image_registry)
        host.launch = ProcessLaunchService(
            process=host.process,
            capabilities=host.capability,
            filesystem=host.filesystem,
            images=host.images,
            image_resource=host.image_registry.resource_for,
            config=host.config,
        )

    @staticmethod
    def _configure_image_boot(host: Runtime) -> None:
        host.image_boot = ImageBootService(
            process=host.process,
            launch=host.launch,
            processes=host.uow.processes,
            audit=host.audit,
            checkpoint=host.checkpoint,
            authority_manifests=host.authority_manifests,
            modules=host.modules,
            tools=host.tools,
            skills=host.skills,
            exec_state=host.process_exec_state,
            checkpoint_installer=host.checkpoint_image_installer,
            package_installer=host.image_package_installer,
            store=host.store,
            owner_instance_id=host.instance_id,
        )
        host.process.add_before_spawn_hook(host.image_boot.preflight_id)
        host.process.add_after_spawn_hook(host.image_boot.configure_spawn)

    @staticmethod
    def _load_extensions(
        host: Runtime,
        *,
        startup_module_manifests: list[str | Path] | tuple[str | Path, ...] | None,
        trusted_modules: list[str] | tuple[str, ...] | None,
        trusted_module_sha256: list[str] | tuple[str, ...] | None,
    ) -> None:
        host.modules.load_core_module()
        host.modules.load_startup_modules(
            startup_module_manifests,
            trusted_modules=trusted_modules,
            trusted_sha256=trusted_module_sha256,
        )
        host.image_registry.load_persisted_images()
        host.tools.rehydrate_registered_jit_tools()

    @staticmethod
    def _install_operation_boundaries(host: Runtime) -> None:
        components = {
            "authority_manifests": host.authority_manifests,
            "capability": host.capability,
            "checkpoint": host.checkpoint,
            "clock": host.clock,
            "filesystem": host.filesystem,
            "human": host.human,
            "image_registry": host.image_registry,
            "jsonrpc": host.jsonrpc,
            "mcp": host.mcp,
            "memory": host.memory,
            "messages": host.messages,
            "object_tasks": host.object_tasks,
            "process": host.process,
            "shell": host.shell,
            "skills": host.skills,
            "tools": host.tools,
        }
        installed = install_explain_boundaries(
            components=components,
            operations=host.operations,
            descriptors=EXPLAIN_BOUNDARY_DESCRIPTORS,
            admission=host.lifecycle,
        )
        host.explainable_boundary_names = frozenset(
            installed | host.external_primitive_boundary_names
        )

    @staticmethod
    def _cleanup_failed_assembly(host: Runtime) -> list[dict[str, str]]:
        lifecycle = getattr(host, "lifecycle", None)
        if lifecycle is not None:
            return lifecycle.cleanup_failed_assembly()
        errors: list[dict[str, str]] = []
        component = getattr(host, "substrate", None)
        try:
            RuntimeLifecycle.shutdown_component(component)
        except Exception as exc:
            errors.append(
                {
                    "component": "substrate",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        return errors

    @classmethod
    def configured(
        cls,
        runtime_type: type[RuntimeT],
        *,
        config: AgentLibOSConfig | None = None,
        substrate: ResourceProviderSubstrate | None = None,
        module_manifests: list[str | Path] | tuple[str | Path, ...] | None = None,
        trusted_modules: list[str] | tuple[str, ...] | None = None,
        trusted_module_sha256: list[str] | tuple[str, ...] | None = None,
    ) -> "RuntimeBuilder[RuntimeT]":
        return cls(
            runtime_type=runtime_type,
            config=config or DEFAULT_CONFIG,
            substrate=substrate,
            module_manifests=(
                tuple(module_manifests)
                if module_manifests is not None
                else None
            ),
            trusted_modules=(
                tuple(trusted_modules)
                if trusted_modules is not None
                else None
            ),
            trusted_module_sha256=(
                tuple(trusted_module_sha256)
                if trusted_module_sha256 is not None
                else None
            ),
        )


__all__ = ["RuntimeBuilder"]

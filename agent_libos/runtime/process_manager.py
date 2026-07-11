from __future__ import annotations

import builtins
import contextlib
from collections.abc import Callable
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models.exceptions import CapabilityDenied, NotFound, ProcessError, ProcessWaitRequired
from agent_libos.utils.ids import new_id, utc_now
from agent_libos.memory.object_memory import ObjectMemoryManager
from agent_libos.models import (
    AgentProcess,
    CapabilityRight,
    EventType,
    ForkMode,
    MemoryView,
    MemoryViewSpec,
    MergePolicy,
    MergeResult,
    ObjectHandle,
    ObjectMetadata,
    ObjectType,
    ProcessResult,
    ProcessSignal,
    ProcessStatus,
    ResourceBudget,
    ResourceUsage,
    ViewMode,
)
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.storage import RuntimeStore

class ProcessManager:
    """Process lifecycle primitive."""

    TERMINAL_STATUSES = {ProcessStatus.EXITED, ProcessStatus.FAILED, ProcessStatus.KILLED}

    def __init__(
        self,
        store: RuntimeStore,
        memory: ObjectMemoryManager,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        config: AgentLibOSConfig | None = None,
        resources: Any | None = None,
        llm_profile_resolver: Callable[[str, str | None], str] | None = None,
        authority_manifests: Any | None = None,
    ):
        self.config = config or DEFAULT_CONFIG
        self.store = store
        self.memory = memory
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        self.resources = resources
        self._llm_profile_resolver = llm_profile_resolver
        self.authority_manifests = authority_manifests
        self._after_spawn_hooks: builtins.list[Callable[[str, str], None]] = []
        self._object_task_terminal_notifier: Callable[[str], None] | None = None

    def add_after_spawn_hook(self, hook: Callable[[str, str], None]) -> None:
        self._after_spawn_hooks.append(hook)

    def bind_object_task_terminal_notifier(self, notifier: Callable[[str], None]) -> None:
        self._object_task_terminal_notifier = notifier

    def spawn(
        self,
        image: str | None = None,
        goal: dict[str, Any] | str | ObjectHandle | None = None,
        capabilities: builtins.list[dict[str, Any]] | None = None,
        resource_budget: ResourceBudget | None = None,
        working_directory: str | None = None,
        llm_profile_id: str | None = None,
        authority_manifest: Any | None = None,
    ) -> str:
        now = utc_now()
        pid = new_id("pid")
        selected_image = image or self.config.runtime.default_image_id
        selected_llm_profile = self._resolve_root_llm_profile(selected_image, llm_profile_id)
        cwd = self._normalize_working_directory(working_directory or self.config.process.default_working_directory)
        try:
            process = AgentProcess(
                pid=pid,
                parent_pid=None,
                image_id=selected_image,
                status=ProcessStatus.CREATED,
                goal_oid=None,
                memory_view=None,
                capabilities=[],
                loaded_skills={},
                tool_table={},
                event_cursor=None,
                checkpoint_head=None,
                resource_budget=resource_budget or self._default_resource_budget(),
                resource_usage=ResourceUsage(),
                created_at=now,
                updated_at=now,
                working_directory=cwd,
                llm_profile_id=selected_llm_profile,
            )
            self.store.insert_process(process)
            self.memory.ensure_process_namespace(pid)
            goal_handle = self._ensure_goal(pid, goal)
            # A process starts with a mutable view rooted at its goal. Later tool
            # results are appended to this view by the LLM executor.
            view = self.memory.create_view(pid, [goal_handle], mode=ViewMode.MUTABLE)
            process.goal_oid = goal_handle.oid
            process.memory_view = view
            process.updated_at = utc_now()
            self.store.update_process(process)
            if self.authority_manifests is not None:
                manifest = self.authority_manifests.prepare_launch(
                    pid=pid,
                    image_id=selected_image,
                    goal_ref=goal_handle.oid,
                    supplied=authority_manifest,
                    authorized_capabilities=capabilities or [],
                    resource_budget=resource_budget,
                    issued_by="process.spawn",
                )
                if manifest.resource_budget:
                    process = self._get(pid)
                    process.resource_budget = ResourceBudget(**manifest.resource_budget)
                    process.updated_at = utc_now()
                    self.store.update_process(process)
                self.authority_manifests.compile_root_capabilities(manifest)
            else:
                self._grant_specs(pid, capabilities or [], issued_by="process.spawn")
            self._run_after_spawn_hooks(pid, selected_image)
            process = self._get(pid)
            process.status = ProcessStatus.RUNNABLE
            process.updated_at = utc_now()
            self.store.update_process(process)
            self.events.emit(
                EventType.PROCESS_CREATED,
                source="runtime",
                target=pid,
                payload={
                    "pid": pid,
                    "image": selected_image,
                    "goal_oid": goal_handle.oid,
                    "working_directory": cwd,
                    "llm_profile_id": selected_llm_profile,
                },
            )
            self.audit.record(
                actor="runtime",
                action="process.spawn",
                target=f"process:{pid}",
                output_refs=[goal_handle.oid],
                decision={"image": selected_image, "working_directory": cwd, "llm_profile_id": selected_llm_profile},
            )
            return pid
        except Exception:
            self._cleanup_failed_launch(pid)
            raise

    def fork(
        self,
        parent: str,
        goal: dict[str, Any] | str | ObjectHandle,
        memory_view: MemoryView | MemoryViewSpec | None = None,
        capabilities: builtins.list[dict[str, Any]] | None = None,
        inherit_capabilities: builtins.list[dict[str, Any]] | None = None,
        resource_budget: ResourceBudget | dict[str, Any] | None = None,
        image: str | None = None,
        mode: ForkMode | str = ForkMode.RESTRICTED,
        working_directory: str | None = None,
        llm_profile_id: str | None = None,
        authority_manifest: Any | None = None,
    ) -> str:
        parent_proc = self._get(parent)
        fork_mode = ForkMode(mode)
        if parent_proc.status in self.TERMINAL_STATUSES:
            raise ProcessError(f"cannot fork terminated process: {parent}")
        self._require_child_budget(parent_proc)
        inherit_specs = inherit_capabilities or []
        self._validate_inherit_capability_specs(parent, inherit_specs)
        selected_budget = self._select_child_resource_budget(parent_proc, resource_budget)
        cwd = self._normalize_working_directory(working_directory or parent_proc.working_directory)
        selected_llm_profile = self._resolve_child_llm_profile(parent_proc, llm_profile_id)
        now = utc_now()
        child_pid = new_id("pid")
        self._reserve_child_budget(parent, child_pid, selected_budget)
        try:
            child = AgentProcess(
                pid=child_pid,
                parent_pid=parent,
                image_id=image or parent_proc.image_id,
                status=ProcessStatus.CREATED,
                goal_oid=None,
                memory_view=None,
                capabilities=[],
                loaded_skills=dict(parent_proc.loaded_skills),
                tool_table={},
                event_cursor=None,
                checkpoint_head=None,
                resource_budget=selected_budget,
                resource_usage=ResourceUsage(),
                created_at=now,
                updated_at=now,
                working_directory=cwd,
                llm_profile_id=selected_llm_profile,
            )
            self.store.insert_process(child)
            self.memory.ensure_process_namespace(child_pid, parent_pid=parent)
            goal_handle = self._ensure_goal(child_pid, goal)
            source_view = parent_proc.memory_view or self.memory.create_view(parent, [], mode=ViewMode.READ_ONLY)
            if isinstance(memory_view, MemoryView):
                source_view = memory_view
                spec = MemoryViewSpec(mode=self._fork_mode_to_view_mode(fork_mode))
            else:
                spec = memory_view or MemoryViewSpec(mode=self._fork_mode_to_view_mode(fork_mode))
            # Forking attenuates memory handles by default. The child can see only
            # roots selected by the parent and only the rights granted into its view.
            child_view = self.memory.fork_view(parent, child_pid, source_view, spec)
            child_view.roots.append(goal_handle)
            child.goal_oid = goal_handle.oid
            child.memory_view = child_view
            child.updated_at = utc_now()
            self.store.update_process(child)
            requested_specs = [*(capabilities or []), *inherit_specs]
            manifest = None
            if self.authority_manifests is not None:
                manifest = self.authority_manifests.prepare_launch(
                    pid=child_pid,
                    image_id=child.image_id,
                    goal_ref=goal_handle.oid,
                    supplied=authority_manifest,
                    authorized_capabilities=requested_specs,
                    resource_budget=selected_budget,
                    parent_pid=parent,
                    issued_by=f"process.fork:{parent}",
                )
            self._compile_child_authority(
                parent_pid=parent,
                child_pid=child_pid,
                manifest=manifest,
                requested_capabilities=capabilities or [],
                inherit_specs=inherit_specs,
                transition_kind="process.fork",
            )
            self._run_after_spawn_hooks(child_pid, child.image_id)
            self._charge_child_creation(parent)
            child = self._get(child_pid)
            child.status = ProcessStatus.RUNNABLE
            child.updated_at = utc_now()
            self.store.update_process(child)
            self.events.emit(
                EventType.PROCESS_FORKED,
                source=parent,
                target=child_pid,
                payload={
                    "parent": parent,
                    "child": child_pid,
                    "mode": fork_mode.value,
                    "working_directory": cwd,
                    "llm_profile_id": selected_llm_profile,
                },
            )
            self.audit.record(
                actor=parent,
                action="process.fork",
                target=f"process:{child_pid}",
                input_refs=[parent_proc.goal_oid] if parent_proc.goal_oid else [],
                output_refs=[goal_handle.oid],
                decision={
                    "mode": fork_mode.value,
                    "image": child.image_id,
                    "working_directory": child.working_directory,
                    "llm_profile_id": selected_llm_profile,
                },
            )
            return child_pid
        except Exception:
            self._cleanup_failed_launch(child_pid)
            self._release_child_budget(child_pid)
            raise

    def spawn_child(
        self,
        parent: str,
        goal: dict[str, Any] | str | ObjectHandle,
        capabilities: builtins.list[dict[str, Any]] | None = None,
        inherit_capabilities: builtins.list[dict[str, Any]] | None = None,
        resource_budget: ResourceBudget | dict[str, Any] | None = None,
        image: str | None = None,
        working_directory: str | None = None,
        initial_status: ProcessStatus | str = ProcessStatus.RUNNABLE,
        llm_profile_id: str | None = None,
        authority_manifest: Any | None = None,
    ) -> str:
        parent_proc = self._get(parent)
        if parent_proc.status in self.TERMINAL_STATUSES:
            raise ProcessError(f"cannot spawn child from terminated process: {parent}")
        self._require_child_budget(parent_proc)
        inherit_specs = inherit_capabilities or []
        self._validate_inherit_capability_specs(parent, inherit_specs)
        selected_budget = self._select_child_resource_budget(parent_proc, resource_budget)
        selected_initial_status = ProcessStatus(initial_status)
        if selected_initial_status in self.TERMINAL_STATUSES:
            raise ProcessError(f"spawn_child initial_status cannot be terminal: {selected_initial_status.value}")
        cwd = self._normalize_working_directory(working_directory or parent_proc.working_directory)
        selected_llm_profile = self._resolve_child_llm_profile(parent_proc, llm_profile_id)
        now = utc_now()
        child_pid = new_id("pid")
        self._reserve_child_budget(parent, child_pid, selected_budget)
        try:
            child = AgentProcess(
                pid=child_pid,
                parent_pid=parent,
                image_id=image or parent_proc.image_id,
                status=ProcessStatus.CREATED,
                goal_oid=None,
                memory_view=None,
                capabilities=[],
                loaded_skills={},
                tool_table={},
                event_cursor=None,
                checkpoint_head=None,
                resource_budget=selected_budget,
                resource_usage=ResourceUsage(),
                created_at=now,
                updated_at=now,
                working_directory=cwd,
                llm_profile_id=selected_llm_profile,
            )
            self.store.insert_process(child)
            self.memory.ensure_process_namespace(child_pid, parent_pid=parent)
            goal_handle = self._ensure_goal(child_pid, goal)
            # Unlike fork(), spawn_child() starts from a fresh address-space-like
            # Object Memory view rooted only at the child goal.
            child.memory_view = self.memory.create_view(child_pid, [goal_handle], mode=ViewMode.MUTABLE)
            child.goal_oid = goal_handle.oid
            child.updated_at = utc_now()
            self.store.update_process(child)
            requested_specs = [*(capabilities or []), *inherit_specs]
            manifest = None
            if self.authority_manifests is not None:
                manifest = self.authority_manifests.prepare_launch(
                    pid=child_pid,
                    image_id=child.image_id,
                    goal_ref=goal_handle.oid,
                    supplied=authority_manifest,
                    authorized_capabilities=requested_specs,
                    resource_budget=selected_budget,
                    parent_pid=parent,
                    issued_by=f"process.spawn_child:{parent}",
                )
            self._compile_child_authority(
                parent_pid=parent,
                child_pid=child_pid,
                manifest=manifest,
                requested_capabilities=capabilities or [],
                inherit_specs=inherit_specs,
                transition_kind="process.spawn_child",
            )
            self._run_after_spawn_hooks(child_pid, child.image_id)
            self._charge_child_creation(parent)
            child = self._get(child_pid)
            child.status = selected_initial_status
            child.updated_at = utc_now()
            self.store.update_process(child)
            self.events.emit(
                EventType.PROCESS_CREATED,
                source=parent,
                target=child_pid,
                payload={
                    "parent": parent,
                    "child": child_pid,
                    "image": child.image_id,
                    "goal_oid": goal_handle.oid,
                    "working_directory": child.working_directory,
                    "status": child.status.value,
                    "llm_profile_id": selected_llm_profile,
                },
            )
            self.audit.record(
                actor=parent,
                action="process.spawn_child",
                target=f"process:{child_pid}",
                output_refs=[goal_handle.oid],
                decision={
                    "image": child.image_id,
                    "working_directory": child.working_directory,
                    "status": child.status.value,
                    "llm_profile_id": selected_llm_profile,
                },
            )
            return child_pid
        except Exception:
            self._cleanup_failed_launch(child_pid)
            self._release_child_budget(child_pid)
            raise

    def exec(
        self,
        pid: str,
        image: str,
        args: dict[str, Any] | None = None,
        goal: dict[str, Any] | str | ObjectHandle | None = None,
        preserve_memory: bool = True,
        preserve_capabilities: bool = False,
        llm_profile_id: str | None = None,
    ) -> None:
        with self.store.transaction(include_object_payloads=True):
            self._exec_uncommitted(
                pid,
                image,
                args=args,
                goal=goal,
                preserve_memory=preserve_memory,
                preserve_capabilities=preserve_capabilities,
                llm_profile_id=llm_profile_id,
            )

    def _exec_uncommitted(
        self,
        pid: str,
        image: str,
        args: dict[str, Any] | None = None,
        goal: dict[str, Any] | str | ObjectHandle | None = None,
        preserve_memory: bool = True,
        preserve_capabilities: bool = False,
        llm_profile_id: str | None = None,
    ) -> None:
        process = self._get(pid)
        if process.status in self.TERMINAL_STATUSES:
            raise ProcessError(f"cannot exec terminated process: {pid}")
        old_image = process.image_id
        goal_handle = self._ensure_goal(pid, goal) if goal is not None else None
        process = self._get(pid)
        if not preserve_capabilities:
            kept: builtins.list[str] = []
            process_namespace_resource = f"object_namespace:{self.memory.process_namespace(pid)}"
            for cap in self.capabilities.capabilities_for(pid):
                if cap.resource.startswith("object:") or cap.resource == process_namespace_resource:
                    kept.append(cap.cap_id)
                else:
                    self.capabilities.revoke(
                        cap.cap_id,
                        revoked_by="process.exec",
                        reason="exec capability shrink",
                        require_authority=False,
                    )
            process = self._get(pid)
            process.capabilities = kept
        if goal_handle is not None:
            process.goal_oid = goal_handle.oid
            if preserve_memory:
                self._add_handle_to_process_view(process, goal_handle)
                process = self._get(pid)
            else:
                process.memory_view = self.memory.create_view(pid, [goal_handle], mode=ViewMode.MUTABLE)
        elif not preserve_memory:
            process.memory_view = None
        process.image_id = image
        if llm_profile_id is not None:
            process.llm_profile_id = self._normalize_llm_profile_id(llm_profile_id)
        process.loaded_skills = {}
        process.updated_at = utc_now()
        self.store.update_process(process)
        self.events.emit(
            EventType.PROCESS_EXEC,
            source=pid,
            target=pid,
            payload={
                "old_image": old_image,
                "new_image": image,
                "preserve_memory": preserve_memory,
                "preserve_capabilities": preserve_capabilities,
                "goal_oid": goal_handle.oid if goal_handle is not None else process.goal_oid,
                "working_directory": process.working_directory,
                "llm_profile_id": process.llm_profile_id,
            },
        )
        self.audit.record(
            actor=pid,
            action="process.exec",
            target=f"process:{pid}",
            output_refs=[goal_handle.oid] if goal_handle is not None else [],
            decision={
                "old_image": old_image,
                "new_image": image,
                "args": args or {},
                "goal_oid": goal_handle.oid if goal_handle is not None else process.goal_oid,
                "preserve_memory": preserve_memory,
                "preserve_capabilities": preserve_capabilities,
                "working_directory": process.working_directory,
                "llm_profile_id": process.llm_profile_id,
            },
        )

    def wait(self, pid: str, child: str, timeout: float | None = None) -> ProcessResult:
        with self.store.locked():
            parent = self._get(pid)
            child_proc = self._require_child(parent.pid, child)
            if child_proc.status not in self.TERMINAL_STATUSES:
                if timeout == 0:
                    raise TimeoutError(f"child still running: {child}")
                parent.status = ProcessStatus.WAITING_EVENT
                parent.status_message = f"waiting for {child}"
                parent.updated_at = utc_now()
                self.store.update_process(parent)
                child_proc = self._require_child(parent.pid, child)
                if child_proc.status not in self.TERMINAL_STATUSES:
                    raise ProcessWaitRequired(child_pid=child, message=f"{pid} is waiting for child process {child}")
                parent.status = ProcessStatus.RUNNABLE
                parent.status_message = None
                parent.updated_at = utc_now()
                self.store.update_process(parent)
        result_handle = None
        if child_proc.status_message and child_proc.status_message.startswith("result_oid:"):
            oid = child_proc.status_message.split(":", 1)[1]
            self.memory.preserve_process_owned(child, {oid})
            result_handle = self.capabilities.handle_for_object(
                pid,
                oid,
                {"read", "materialize", "link", "diff"},
                issued_by=f"process.wait:{child}",
            )
            self._add_handle_to_process_view(parent, result_handle)
        if parent.status == ProcessStatus.WAITING_EVENT and parent.status_message == f"waiting for {child}":
            parent.status = ProcessStatus.RUNNABLE
            parent.status_message = None
            parent.updated_at = utc_now()
            self.store.update_process(parent)
        self.audit.record(
            actor=pid,
            action="process.wait",
            target=f"process:{child}",
            output_refs=[result_handle.oid] if result_handle else [],
            decision={"child_status": child_proc.status.value},
        )
        return ProcessResult(pid=child, status=child_proc.status, result=result_handle, message=child_proc.status_message)

    def set_working_directory(self, pid: str, path: str) -> AgentProcess:
        process = self._get(pid)
        if process.status in self.TERMINAL_STATUSES:
            raise ProcessError(f"cannot change working directory for terminated process: {pid}")
        process.working_directory = self._normalize_working_directory(path)
        process.updated_at = utc_now()
        self.store.update_process(process)
        self.audit.record(
            actor=pid,
            action="process.chdir",
            target=f"process:{pid}",
            decision={"working_directory": process.working_directory},
        )
        return process

    def working_directory(self, pid: str) -> str:
        return self._get(pid).working_directory

    def list_children(self, pid: str, include_terminal: bool = True) -> builtins.list[AgentProcess]:
        self._get(pid)
        children = self.store.list_child_processes(pid)
        if not include_terminal:
            children = [process for process in children if process.status not in self.TERMINAL_STATUSES]
        self.audit.record(
            actor=pid,
            action="process.list_children",
            target=f"process:{pid}",
            decision={"count": len(children), "include_terminal": include_terminal},
        )
        return children

    def signal_child(
        self,
        pid: str,
        child: str,
        signal: ProcessSignal | str,
        reason: str | None = None,
    ) -> AgentProcess:
        child_proc = self._require_child(pid, child)
        sig = ProcessSignal(signal)
        self._apply_signal(
            child_proc,
            sig,
            payload={"reason": reason} if reason else {},
            actor=pid,
            action="process.signal_child",
        )
        updated = self._get(child)
        if updated.status in self.TERMINAL_STATUSES:
            self._wake_parent_waiting_on_child(updated)
            self._notify_object_task_process_terminal(updated.pid)
        return updated

    def merge_child_memory(
        self,
        pid: str,
        child: str,
        policy: MergePolicy | None = None,
    ) -> MergeResult:
        child_proc = self._require_child(pid, child)
        if child_proc.status not in self.TERMINAL_STATUSES:
            raise ProcessError(f"cannot merge running child process: {child}")
        if child_proc.memory_view is None:
            return MergeResult(merged_oids=[], skipped_oids=[])
        result = self.memory.merge_view(pid, child_proc.memory_view, policy=policy)
        parent = self._get(pid)
        for handle in result.merged_handles:
            self._add_handle_to_process_view(parent, handle)
        adopted = self.memory.adopt_process_owned(child, pid, result.merged_oids)
        released = self.memory.release_process_owned(child)
        self.audit.record(
            actor=pid,
            action="process.merge_child_memory",
            target=f"process:{child}",
            output_refs=result.merged_oids,
            decision={
                "merged": len(result.merged_oids),
                "skipped": result.skipped_oids,
                "adopted": adopted,
                "released_child_owned": released,
            },
        )
        return result

    def signal(self, target: str, signal: ProcessSignal | str, payload: dict[str, Any] | None = None) -> None:
        proc = self._get(target)
        sig = ProcessSignal(signal)
        self._apply_signal(proc, sig, payload=payload or {}, actor="runtime", action="process.signal")
        updated = self._get(target)
        if updated.status in self.TERMINAL_STATUSES:
            self._wake_parent_waiting_on_child(updated)
            self._notify_object_task_process_terminal(updated.pid)

    def _apply_signal(
        self,
        proc: AgentProcess,
        sig: ProcessSignal,
        payload: dict[str, Any],
        actor: str,
        action: str,
    ) -> None:
        if proc.status in self.TERMINAL_STATUSES:
            raise ProcessError(f"cannot signal terminal process: {proc.pid} status={proc.status.value}")
        if sig == ProcessSignal.PAUSE:
            if proc.status in {ProcessStatus.WAITING_EVENT, ProcessStatus.WAITING_TOOL, ProcessStatus.WAITING_HUMAN}:
                raise ProcessError(f"cannot pause waiting process: {proc.pid} status={proc.status.value}")
            proc.status = ProcessStatus.PAUSED
        elif sig == ProcessSignal.RESUME:
            if proc.status in {ProcessStatus.WAITING_EVENT, ProcessStatus.WAITING_TOOL, ProcessStatus.WAITING_HUMAN}:
                raise ProcessError(f"cannot resume waiting process: {proc.pid} status={proc.status.value}")
            if proc.status in {ProcessStatus.PAUSED, ProcessStatus.SUSPENDED}:
                proc.status = ProcessStatus.RUNNABLE
        elif sig in {ProcessSignal.CANCEL, ProcessSignal.TERMINATE}:
            proc.status = ProcessStatus.KILLED
        proc.status_message = payload.get("reason")
        proc.updated_at = utc_now()
        self.store.update_process(proc)
        if proc.status in self.TERMINAL_STATUSES:
            self._release_child_budget(proc.pid)
            self._finalize_terminal_process(proc, preserve_oids=set())
        self.events.emit(
            EventType.PROCESS_SIGNAL,
            source=actor,
            target=proc.pid,
            payload={"signal": sig.value, "payload": payload or {}},
        )
        self.audit.record(
            actor=actor,
            action=action,
            target=f"process:{proc.pid}",
            decision={"signal": sig.value, "payload": payload or {}},
        )

    def pause(self, pid: str, reason: str) -> None:
        self.signal(pid, ProcessSignal.PAUSE, {"reason": reason})

    def resume(self, pid: str) -> None:
        self.signal(pid, ProcessSignal.RESUME, {})

    def cancel(self, pid: str, reason: str) -> None:
        self.signal(pid, ProcessSignal.CANCEL, {"reason": reason})

    def exit(self, pid: str, result: ObjectHandle | None = None, failed: bool = False, message: str | None = None) -> None:
        process = self._get(pid)
        if process.status in self.TERMINAL_STATUSES:
            self._release_rejected_exit_result(pid, result)
            raise ProcessError(f"cannot exit terminal process: {pid} status={process.status.value}")
        # Terminal state, child-budget release, evidence, and parent wakeup are
        # one durable lifecycle transition.  Host/object finalizers remain
        # post-commit because provider cleanup cannot be rolled back safely.
        with self.memory.ownership_locked(), self.store.transaction(include_object_payloads=True):
            process = self._get(pid)
            if process.status in self.TERMINAL_STATUSES:
                self._release_rejected_exit_result(pid, result)
                raise ProcessError(f"cannot exit terminal process: {pid} status={process.status.value}")
            process.status = ProcessStatus.FAILED if failed else ProcessStatus.EXITED
            process.status_message = f"result_oid:{result.oid}" if result is not None else message
            process.updated_at = utc_now()
            self.store.update_process(process)
            self._release_child_budget(pid)
            self.events.emit(
                EventType.PROCESS_EXITED,
                source=pid,
                target=process.parent_pid,
                payload={"pid": pid, "status": process.status.value, "result_oid": result.oid if result else None},
            )
            self.audit.record(
                actor=pid,
                action="process.exit",
                target=f"process:{pid}",
                output_refs=[result.oid] if result else [],
                decision={"status": process.status.value, "message": message},
            )
            self._wake_parent_waiting_on_child(process)
        self._finalize_terminal_process(process, preserve_oids={result.oid} if result is not None else set())
        self._notify_object_task_process_terminal(process.pid)

    def finalize_killed_processes(self, pids: Iterable[str], *, reason: str) -> None:
        errors: list[str] = []
        for pid in pids:
            process = self.store.get_process(pid)
            if process is None or process.status != ProcessStatus.KILLED:
                continue
            try:
                self._finalize_terminal_process(process, preserve_oids=set())
                self.events.emit(
                    EventType.PROCESS_EXITED,
                    source=pid,
                    target=process.parent_pid,
                    payload={"pid": pid, "status": process.status.value, "result_oid": None, "reason": reason},
                )
                self._notify_object_task_process_terminal(pid)
            except Exception as exc:
                # A resource kill can cover an entire descendant tree. One
                # process' cleanup failure must not strand every later killed
                # process; report the aggregate after attempting them all.
                errors.append(f"{pid}: {type(exc).__name__}: {exc}")
        if errors:
            raise RuntimeError("killed process finalization failed: " + "; ".join(errors))

    def _finalize_terminal_process(self, process: AgentProcess, preserve_oids: set[str]) -> None:
        self._release_terminal_child_memory(process.pid, preserve_oids=preserve_oids)
        if process.parent_pid is None:
            # Root process-owned memory is volatile and is reclaimed immediately.
            # Child process memory is held until the parent can merge or discard
            # it, so merge_child_memory remains meaningful after child exit.
            self.memory.release_process_owned(process.pid, preserve_oids=preserve_oids)

    def _release_terminal_child_memory(self, pid: str, preserve_oids: set[str]) -> None:
        stack = [
            child
            for child in self.store.list_child_processes(pid)
            if child.status in self.TERMINAL_STATUSES
        ]
        terminal_children: builtins.list[AgentProcess] = []
        while stack:
            child = stack.pop()
            if child.status in self.TERMINAL_STATUSES:
                terminal_children.append(child)
                stack.extend(
                    grandchild
                    for grandchild in self.store.list_child_processes(child.pid)
                    if grandchild.status in self.TERMINAL_STATUSES
                )
        for child in reversed(terminal_children):
            self.memory.release_process_owned(child.pid, preserve_oids=preserve_oids)

    def get(self, pid: str) -> AgentProcess:
        return self._get(pid)

    def list(self) -> builtins.list[AgentProcess]:
        return self.store.list_processes()

    def _get(self, pid: str) -> AgentProcess:
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        return process

    def _default_resource_budget(self) -> ResourceBudget:
        defaults = self.config.process
        return ResourceBudget(
            max_tool_calls=defaults.max_tool_calls,
            max_child_processes=defaults.max_child_processes,
            max_runtime_seconds=defaults.max_runtime_seconds,
            max_context_materialization_tokens=defaults.max_context_materialization_tokens,
            max_context_materialization_total_tokens=defaults.max_context_materialization_total_tokens,
            max_llm_calls=defaults.max_llm_calls,
            max_llm_total_tokens=defaults.max_llm_total_tokens,
            max_subprocess_wall_seconds=defaults.max_subprocess_wall_seconds,
            max_subprocess_cpu_seconds=defaults.max_subprocess_cpu_seconds,
            max_subprocess_memory_bytes=defaults.max_subprocess_memory_bytes,
            max_external_read_bytes=defaults.max_external_read_bytes,
            max_external_write_bytes=defaults.max_external_write_bytes,
            max_jsonrpc_bytes=defaults.max_jsonrpc_bytes,
            max_mcp_bytes=defaults.max_mcp_bytes,
            max_deno_syscalls=defaults.max_deno_syscalls,
        )

    def _normalize_working_directory(self, path: str | None) -> str:
        raw = (path or self.config.process.default_working_directory).replace("\\", "/").strip()
        if raw in {"", "."}:
            return "."
        if PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
            raise ProcessError(f"working directory must be workspace-relative: {path}")
        parts: list[str] = []
        for part in raw.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    raise ProcessError(f"working directory escapes workspace root: {path}")
                parts.pop()
                continue
            parts.append(part)
        return "/".join(parts) if parts else "."

    def _resolve_root_llm_profile(self, image_id: str, explicit_profile_id: str | None) -> str:
        if self._llm_profile_resolver is not None:
            return self._llm_profile_resolver(image_id, explicit_profile_id)
        if explicit_profile_id is not None:
            return self._normalize_llm_profile_id(explicit_profile_id)
        return self.config.llm.default_profile_id

    def _resolve_child_llm_profile(self, parent: AgentProcess, explicit_profile_id: str | None) -> str:
        if explicit_profile_id is not None:
            return self._normalize_llm_profile_id(explicit_profile_id)
        return parent.llm_profile_id or self.config.llm.default_profile_id

    def _normalize_llm_profile_id(self, profile_id: str) -> str:
        selected = str(profile_id or "").strip()
        if not selected:
            raise ProcessError("LLM profile id must be a non-empty string")
        return selected

    def _child_resource_budget(self, parent: AgentProcess) -> ResourceBudget:
        budget = self.resources.remaining_budget(parent.pid) if self.resources is not None else parent.resource_budget
        divisor = self.config.process.fork_budget_divisor
        return ResourceBudget(
            max_tool_calls=self._attenuate_int(budget.max_tool_calls, divisor, self.config.process.fork_min_tool_calls),
            max_child_processes=self._attenuate_int(
                budget.max_child_processes,
                divisor,
                self.config.process.fork_min_child_processes,
            ),
            max_runtime_seconds=self._attenuate_float(budget.max_runtime_seconds, divisor),
            max_context_materialization_tokens=budget.max_context_materialization_tokens,
            max_context_materialization_total_tokens=self._attenuate_int(
                budget.max_context_materialization_total_tokens,
                divisor,
                0,
            ),
            max_llm_calls=self._attenuate_int(budget.max_llm_calls, divisor, 0),
            max_llm_total_tokens=self._attenuate_int(budget.max_llm_total_tokens, divisor, 0),
            max_subprocess_wall_seconds=self._attenuate_float(budget.max_subprocess_wall_seconds, divisor),
            max_subprocess_cpu_seconds=self._attenuate_float(budget.max_subprocess_cpu_seconds, divisor),
            max_subprocess_memory_bytes=self._attenuate_int(budget.max_subprocess_memory_bytes, divisor, 0),
            max_external_read_bytes=self._attenuate_int(budget.max_external_read_bytes, divisor, 0),
            max_external_write_bytes=self._attenuate_int(budget.max_external_write_bytes, divisor, 0),
            max_jsonrpc_bytes=self._attenuate_int(budget.max_jsonrpc_bytes, divisor, 0),
            max_mcp_bytes=self._attenuate_int(budget.max_mcp_bytes, divisor, 0),
            max_deno_syscalls=self._attenuate_int(budget.max_deno_syscalls, divisor, 0),
        )

    def _select_child_resource_budget(
        self,
        parent: AgentProcess,
        requested: ResourceBudget | dict[str, Any] | None,
    ) -> ResourceBudget:
        selected = self._coerce_resource_budget(requested) if requested is not None else self._child_resource_budget(parent)
        if self.resources is not None:
            self.resources.validate_child_budget(parent.pid, selected, reserved_usage=ResourceUsage(child_processes=1))
        return selected

    def _reserve_child_budget(self, parent_pid: str, child_pid: str, budget: ResourceBudget) -> None:
        if self.resources is None:
            return
        self.resources.reserve_child_budget(parent_pid, child_pid, budget)

    def _release_child_budget(self, pid: str) -> None:
        if self.resources is None:
            return
        self.resources.release_process_reservations(pid)

    def _charge_child_creation(self, parent_pid: str) -> None:
        if self.resources is None:
            return
        self.resources.charge(
            parent_pid,
            ResourceUsage(child_processes=1),
            source="process.child_create",
            context={"parent_pid": parent_pid},
            allow_overage=False,
            kill_on_exceed=False,
        )

    def _coerce_resource_budget(self, value: ResourceBudget | dict[str, Any]) -> ResourceBudget:
        if isinstance(value, ResourceBudget):
            return value
        if not isinstance(value, dict):
            raise ProcessError("resource_budget must be a mapping")
        allowed = set(ResourceBudget.__dataclass_fields__)
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ProcessError(f"unknown resource_budget fields: {unknown}")
        try:
            return ResourceBudget(**{key: item for key, item in value.items() if key in allowed})
        except ValueError as exc:
            raise ProcessError(str(exc)) from exc

    def _attenuate_int(self, value: int | None, divisor: int, minimum: int) -> int | None:
        if value is None:
            return None
        return max(minimum, int(value) // divisor)

    def _attenuate_float(self, value: float | int | None, divisor: int) -> float | None:
        if value is None:
            return None
        return float(value) / divisor

    def _ensure_goal(self, pid: str, goal: dict[str, Any] | str | ObjectHandle | None) -> ObjectHandle:
        if isinstance(goal, ObjectHandle):
            return goal
        default_goal = self.config.process.default_goal_text
        payload = {"text": goal or default_goal} if isinstance(goal, str) or goal is None else goal
        return self.memory.create_object(
            pid=pid,
            object_type=ObjectType.GOAL,
            payload=payload,
            metadata=ObjectMetadata(title="Process goal", tags=["goal"]),
            immutable=True,
        )

    def _grant_specs(self, pid: str, specs: Iterable[dict[str, Any]], issued_by: str) -> None:
        for spec in specs:
            self.capabilities.grant(
                subject=pid,
                resource=spec["resource"],
                rights=spec.get("rights", [CapabilityRight.READ.value]),
                issued_by=issued_by,
                constraints=spec.get("constraints"),
                expires_at=spec.get("expires_at"),
                delegable=spec.get("delegable", False),
                revocable=spec.get("revocable", True),
            )

    def _compile_child_authority(
        self,
        *,
        parent_pid: str,
        child_pid: str,
        manifest: Any | None,
        requested_capabilities: list[dict[str, Any]],
        inherit_specs: list[dict[str, Any]],
        transition_kind: str,
    ) -> None:
        # The compatibility mode keeps trusted, non-ceiling launch grants on
        # its legacy path. Every manifest-enforced transition derives the final
        # declared authority, not merely the duplicate ``capabilities`` input.
        transition_ceiling = bool(
            manifest is not None and manifest.metadata.get("transition_ceiling")
        )
        if (
            self.config.runtime.launch_authority_mode == "legacy_image_grants"
            and not transition_ceiling
        ):
            if requested_capabilities:
                self._grant_specs(
                    child_pid,
                    requested_capabilities,
                    issued_by=f"{transition_kind}:{parent_pid}",
                )
            if inherit_specs:
                self.capabilities.derive_authority(
                    source_subject=parent_pid,
                    target_subject=child_pid,
                    requested_specs=inherit_specs,
                    transition_kind=f"{transition_kind}.inherit",
                    ceiling_specs=manifest.authorized_capabilities if manifest is not None else None,
                )
            return

        selected_specs = (
            list(manifest.authorized_capabilities)
            if manifest is not None
            else [*requested_capabilities, *inherit_specs]
        )
        if not selected_specs:
            return
        self.capabilities.derive_authority(
            source_subject=parent_pid,
            target_subject=child_pid,
            requested_specs=selected_specs,
            transition_kind=transition_kind,
            ceiling_specs=manifest.authorized_capabilities if manifest is not None else None,
        )

    def _inherit_capability_specs(
        self,
        parent_pid: str,
        child_pid: str,
        specs: Iterable[dict[str, Any]],
        issued_by: str,
    ) -> None:
        for spec in specs:
            self.capabilities.inherit(
                parent=parent_pid,
                child=child_pid,
                resource=spec["resource"],
                rights=spec.get("rights", [CapabilityRight.READ.value]),
                issued_by=issued_by,
                constraints=spec.get("constraints") if isinstance(spec.get("constraints"), dict) else None,
            )

    def _validate_inherit_capability_specs(self, parent_pid: str, specs: Iterable[dict[str, Any]]) -> None:
        for spec in specs:
            try:
                self.capabilities.validate_delegation(parent_pid, spec)
            except CapabilityDenied as exc:
                resource = spec.get("resource")
                rights = spec.get("rights", [CapabilityRight.READ.value])
                raise CapabilityDenied(
                    f"{parent_pid} cannot inherit {rights} on {resource}: {exc}"
                ) from exc

    def _fork_mode_to_view_mode(self, mode: ForkMode) -> ViewMode:
        if mode == ForkMode.COPY:
            return ViewMode.COPY_ON_WRITE
        if mode == ForkMode.SPECULATIVE:
            return ViewMode.EPHEMERAL
        if mode == ForkMode.WORKER:
            return ViewMode.READ_ONLY
        return ViewMode.READ_ONLY

    def _run_after_spawn_hooks(self, pid: str, image_id: str) -> None:
        for hook in self._after_spawn_hooks:
            hook(pid, image_id)

    def _cleanup_failed_launch(self, pid: str) -> None:
        with contextlib.suppress(Exception):
            self.memory.release_process_owned(pid)
        namespace = self.memory.process_namespace(pid)
        namespace_resource = f"object_namespace:{namespace}"
        with contextlib.suppress(Exception):
            with self.store.transaction(include_object_payloads=True) as cur:
                cur.execute("DELETE FROM capabilities WHERE subject = ? OR resource = ?", (pid, namespace_resource))
                cur.execute("DELETE FROM process_resource_reservations WHERE parent_pid = ? OR child_pid = ?", (pid, pid))
                cur.execute("DELETE FROM llm_pending_actions WHERE pid = ?", (pid,))
                cur.execute("DELETE FROM authority_manifests WHERE pid = ?", (pid,))
                cur.execute("DELETE FROM tool_candidates WHERE pid = ?", (pid,))
                cur.execute("DELETE FROM process_messages WHERE sender = ? OR recipient_pid = ?", (pid, pid))
                cur.execute("DELETE FROM object_namespaces WHERE namespace = ? AND created_by = ?", (namespace, pid))
                cur.execute("DELETE FROM processes WHERE pid = ?", (pid,))

    def _release_rejected_exit_result(self, pid: str, result: ObjectHandle | None) -> None:
        if result is None:
            return
        obj = self.store.get_object(result.oid)
        if obj is None or obj.owner_kind != ObjectOwnerKind.PROCESS or obj.owner_id != pid:
            return
        self.memory.delete_object_trusted("process", result.oid, reason="terminal_exit_rejected")

    def _require_child(self, parent: str, child: str) -> AgentProcess:
        self._get(parent)
        child_proc = self._get(child)
        if child_proc.parent_pid != parent:
            raise ProcessError(f"{child} is not a child of {parent}")
        return child_proc

    def _require_child_budget(self, parent: AgentProcess) -> None:
        if self.resources is not None:
            return
        if parent.resource_budget.max_child_processes is None:
            return
        child_count = len(self.store.list_child_processes(parent.pid))
        if child_count >= parent.resource_budget.max_child_processes:
            raise ProcessError(
                f"process {parent.pid} exhausted child process budget: "
                f"{child_count}/{parent.resource_budget.max_child_processes}"
            )

    def _add_handle_to_process_view(self, process: AgentProcess, handle: ObjectHandle) -> None:
        if process.memory_view is None:
            process.memory_view = self.memory.create_view(process.pid, [handle], mode=ViewMode.READ_ONLY)
        elif all(existing.oid != handle.oid for existing in process.memory_view.roots):
            process.memory_view.roots.append(handle)
        process.updated_at = utc_now()
        self.store.update_process(process)

    def _wake_parent_waiting_on_child(self, child: AgentProcess) -> None:
        if child.parent_pid is None:
            return
        parent = self.store.get_process(child.parent_pid)
        if parent is None:
            return
        if parent.status != ProcessStatus.WAITING_EVENT:
            return
        if parent.status_message != f"waiting for {child.pid}":
            return
        parent.status = ProcessStatus.RUNNABLE
        parent.status_message = None
        parent.updated_at = utc_now()
        self.store.update_process(parent)
        self.audit.record(
            actor="process",
            action="process.wait_wake",
            target=f"process:{parent.pid}",
            decision={"child": child.pid, "child_status": child.status.value},
        )

    def _notify_object_task_process_terminal(self, pid: str) -> None:
        if self._object_task_terminal_notifier is None:
            return
        self._object_task_terminal_notifier(pid)

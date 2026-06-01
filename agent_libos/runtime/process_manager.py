from __future__ import annotations

import builtins
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
    ViewMode,
)
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.storage import SQLiteStore

_RUNTIME_DEFAULTS = DEFAULT_CONFIG.runtime


class ProcessManager:
    """Process lifecycle primitive."""

    TERMINAL_STATUSES = {ProcessStatus.EXITED, ProcessStatus.FAILED, ProcessStatus.KILLED}

    def __init__(
        self,
        store: SQLiteStore,
        memory: ObjectMemoryManager,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        config: AgentLibOSConfig | None = None,
    ):
        self.config = config or DEFAULT_CONFIG
        self.store = store
        self.memory = memory
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        self._after_spawn_hooks: builtins.list[Callable[[str, str], None]] = []

    def add_after_spawn_hook(self, hook: Callable[[str, str], None]) -> None:
        self._after_spawn_hooks.append(hook)

    def spawn(
        self,
        image: str = _RUNTIME_DEFAULTS.default_image_id,
        goal: dict[str, Any] | str | ObjectHandle | None = None,
        capabilities: builtins.list[dict[str, Any]] | None = None,
        resource_budget: ResourceBudget | None = None,
        working_directory: str | None = None,
    ) -> str:
        now = utc_now()
        pid = new_id("pid")
        cwd = self._normalize_working_directory(working_directory or self.config.process.default_working_directory)
        process = AgentProcess(
            pid=pid,
            parent_pid=None,
            image_id=image,
            status=ProcessStatus.CREATED,
            goal_oid=None,
            memory_view=None,
            capabilities=[],
            loaded_skills={},
            tool_table={},
            event_cursor=None,
            checkpoint_head=None,
            resource_budget=resource_budget or self._default_resource_budget(),
            created_at=now,
            updated_at=now,
            working_directory=cwd,
        )
        self.store.insert_process(process)
        self.memory.ensure_process_namespace(pid)
        goal_handle = self._ensure_goal(pid, goal)
        # A process starts with a mutable view rooted at its goal. Later tool
        # results are appended to this view by the LLM executor.
        view = self.memory.create_view(pid, [goal_handle], mode=ViewMode.MUTABLE)
        process.goal_oid = goal_handle.oid
        process.memory_view = view
        process.status = ProcessStatus.RUNNABLE
        process.updated_at = utc_now()
        self.store.update_process(process)
        self._grant_specs(pid, capabilities or [], issued_by="process.spawn")
        self.events.emit(
            EventType.PROCESS_CREATED,
            source="runtime",
            target=pid,
            payload={"pid": pid, "image": image, "goal_oid": goal_handle.oid, "working_directory": cwd},
        )
        self.audit.record(
            actor="runtime",
            action="process.spawn",
            target=f"process:{pid}",
            output_refs=[goal_handle.oid],
            decision={"image": image, "working_directory": cwd},
        )
        self._run_after_spawn_hooks(pid, image)
        return pid

    def fork(
        self,
        parent: str,
        goal: dict[str, Any] | str | ObjectHandle,
        memory_view: MemoryView | MemoryViewSpec | None = None,
        capabilities: builtins.list[dict[str, Any]] | None = None,
        inherit_capabilities: builtins.list[dict[str, Any]] | None = None,
        image: str | None = None,
        mode: ForkMode | str = ForkMode.RESTRICTED,
        working_directory: str | None = None,
    ) -> str:
        parent_proc = self._get(parent)
        fork_mode = ForkMode(mode)
        if parent_proc.status in self.TERMINAL_STATUSES:
            raise ProcessError(f"cannot fork terminated process: {parent}")
        self._require_child_budget(parent_proc)
        inherit_specs = inherit_capabilities or []
        self._validate_inherit_capability_specs(parent, inherit_specs)
        cwd = self._normalize_working_directory(working_directory or parent_proc.working_directory)
        now = utc_now()
        child_pid = new_id("pid")
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
            resource_budget=ResourceBudget(
                max_tool_calls=max(
                    self.config.process.fork_min_tool_calls,
                    parent_proc.resource_budget.max_tool_calls // self.config.process.fork_budget_divisor,
                ),
                max_child_processes=max(
                    self.config.process.fork_min_child_processes,
                    parent_proc.resource_budget.max_child_processes // self.config.process.fork_budget_divisor,
                ),
                max_runtime_seconds=parent_proc.resource_budget.max_runtime_seconds,
                max_materialized_tokens=parent_proc.resource_budget.max_materialized_tokens,
            ),
            created_at=now,
            updated_at=now,
            working_directory=cwd,
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
        child.status = ProcessStatus.RUNNABLE
        child.updated_at = utc_now()
        self.store.update_process(child)
        self._grant_specs(child_pid, capabilities or [], issued_by=f"process.fork:{parent}")
        self._inherit_capability_specs(
            parent_pid=parent,
            child_pid=child_pid,
            specs=inherit_specs,
            issued_by=f"process.fork:{parent}",
        )
        self.events.emit(
            EventType.PROCESS_FORKED,
            source=parent,
            target=child_pid,
            payload={"parent": parent, "child": child_pid, "mode": fork_mode.value, "working_directory": cwd},
        )
        self.audit.record(
            actor=parent,
            action="process.fork",
            target=f"process:{child_pid}",
            input_refs=[parent_proc.goal_oid] if parent_proc.goal_oid else [],
            output_refs=[goal_handle.oid],
            decision={"mode": fork_mode.value, "image": child.image_id, "working_directory": child.working_directory},
        )
        self._run_after_spawn_hooks(child_pid, child.image_id)
        return child_pid

    def spawn_child(
        self,
        parent: str,
        goal: dict[str, Any] | str | ObjectHandle,
        capabilities: builtins.list[dict[str, Any]] | None = None,
        inherit_capabilities: builtins.list[dict[str, Any]] | None = None,
        image: str | None = None,
        working_directory: str | None = None,
    ) -> str:
        parent_proc = self._get(parent)
        if parent_proc.status in self.TERMINAL_STATUSES:
            raise ProcessError(f"cannot spawn child from terminated process: {parent}")
        self._require_child_budget(parent_proc)
        inherit_specs = inherit_capabilities or []
        self._validate_inherit_capability_specs(parent, inherit_specs)
        cwd = self._normalize_working_directory(working_directory or parent_proc.working_directory)
        now = utc_now()
        child_pid = new_id("pid")
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
            resource_budget=self._child_resource_budget(parent_proc),
            created_at=now,
            updated_at=now,
            working_directory=cwd,
        )
        self.store.insert_process(child)
        self.memory.ensure_process_namespace(child_pid, parent_pid=parent)
        goal_handle = self._ensure_goal(child_pid, goal)
        # Unlike fork(), spawn_child() starts from a fresh address-space-like
        # Object Memory view rooted only at the child goal.
        child.memory_view = self.memory.create_view(child_pid, [goal_handle], mode=ViewMode.MUTABLE)
        child.goal_oid = goal_handle.oid
        child.status = ProcessStatus.RUNNABLE
        child.updated_at = utc_now()
        self.store.update_process(child)
        self._grant_specs(child_pid, capabilities or [], issued_by=f"process.spawn_child:{parent}")
        self._inherit_capability_specs(
            parent_pid=parent,
            child_pid=child_pid,
            specs=inherit_specs,
            issued_by=f"process.spawn_child:{parent}",
        )
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
            },
        )
        self.audit.record(
            actor=parent,
            action="process.spawn_child",
            target=f"process:{child_pid}",
            output_refs=[goal_handle.oid],
            decision={"image": child.image_id, "working_directory": child.working_directory},
        )
        self._run_after_spawn_hooks(child_pid, child.image_id)
        return child_pid

    def exec(
        self,
        pid: str,
        image: str,
        args: dict[str, Any] | None = None,
        goal: dict[str, Any] | str | ObjectHandle | None = None,
        preserve_memory: bool = True,
        preserve_capabilities: bool = False,
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
                    self.capabilities.revoke(cap.cap_id, revoked_by="process.exec", reason="exec capability shrink")
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
            },
        )

    def wait(self, pid: str, child: str, timeout: float | None = None) -> ProcessResult:
        parent = self._get(pid)
        child_proc = self._require_child(parent.pid, child)
        if child_proc.status not in self.TERMINAL_STATUSES:
            parent.status = ProcessStatus.WAITING_EVENT
            parent.status_message = f"waiting for {child}"
            parent.updated_at = utc_now()
            self.store.update_process(parent)
            if timeout == 0:
                raise TimeoutError(f"child still running: {child}")
            raise ProcessWaitRequired(child_pid=child, message=f"{pid} is waiting for child process {child}")
        result_handle = None
        if child_proc.status_message and child_proc.status_message.startswith("result_oid:"):
            oid = child_proc.status_message.split(":", 1)[1]
            result_handle = self.capabilities.handle_for_object(
                pid,
                oid,
                {"read", "materialize", "link", "diff"},
                issued_by=f"process.wait:{child}",
            )
            self._add_handle_to_process_view(parent, result_handle)
        if parent.status == ProcessStatus.WAITING_EVENT:
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
        children = [process for process in self.store.list_processes() if process.parent_pid == pid]
        if not include_terminal:
            children = [process for process in children if process.status not in self.TERMINAL_STATUSES]
        children.sort(key=lambda process: process.created_at)
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
        for oid in result.merged_oids:
            handle = self.capabilities.handle_for_object(
                pid,
                oid,
                {"read", "materialize", "link", "diff"},
                issued_by=f"process.merge_child_memory:{child}",
            )
            self._add_handle_to_process_view(parent, handle)
        self.audit.record(
            actor=pid,
            action="process.merge_child_memory",
            target=f"process:{child}",
            output_refs=result.merged_oids,
            decision={"merged": len(result.merged_oids), "skipped": result.skipped_oids},
        )
        return result

    def signal(self, target: str, signal: ProcessSignal | str, payload: dict[str, Any] | None = None) -> None:
        proc = self._get(target)
        sig = ProcessSignal(signal)
        self._apply_signal(proc, sig, payload=payload or {}, actor="runtime", action="process.signal")
        updated = self._get(target)
        if updated.status in self.TERMINAL_STATUSES:
            self._wake_parent_waiting_on_child(updated)

    def _apply_signal(
        self,
        proc: AgentProcess,
        sig: ProcessSignal,
        payload: dict[str, Any],
        actor: str,
        action: str,
    ) -> None:
        if sig == ProcessSignal.PAUSE:
            proc.status = ProcessStatus.PAUSED
        elif sig == ProcessSignal.RESUME:
            proc.status = ProcessStatus.RUNNABLE
        elif sig in {ProcessSignal.CANCEL, ProcessSignal.TERMINATE}:
            proc.status = ProcessStatus.KILLED
        proc.status_message = payload.get("reason")
        proc.updated_at = utc_now()
        self.store.update_process(proc)
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
        process.status = ProcessStatus.FAILED if failed else ProcessStatus.EXITED
        process.status_message = f"result_oid:{result.oid}" if result is not None else message
        process.updated_at = utc_now()
        self.store.update_process(process)
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
        # Reclaim volatile Object Memory owned by this process after its final
        # state has been recorded.
        self.memory.release_process_owned(pid, preserve_oids={result.oid} if result is not None else set())
        self._wake_parent_waiting_on_child(process)

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
            max_materialized_tokens=defaults.max_materialized_tokens,
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

    def _child_resource_budget(self, parent: AgentProcess) -> ResourceBudget:
        return ResourceBudget(
            max_tool_calls=max(
                self.config.process.fork_min_tool_calls,
                parent.resource_budget.max_tool_calls // self.config.process.fork_budget_divisor,
            ),
            max_child_processes=max(
                self.config.process.fork_min_child_processes,
                parent.resource_budget.max_child_processes // self.config.process.fork_budget_divisor,
            ),
            max_runtime_seconds=parent.resource_budget.max_runtime_seconds,
            max_materialized_tokens=parent.resource_budget.max_materialized_tokens,
        )

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
            resource = spec["resource"]
            rights = spec.get("rights", [CapabilityRight.READ.value])
            for right in rights:
                policy = self.capabilities.permission_policy(parent_pid, resource, right)
                if policy != CapabilityManager.ALWAYS_ALLOW:
                    raise CapabilityDenied(
                        f"{parent_pid} cannot inherit {right} on {resource}; parent policy is {policy}"
                    )

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

    def _require_child(self, parent: str, child: str) -> AgentProcess:
        self._get(parent)
        child_proc = self._get(child)
        if child_proc.parent_pid != parent:
            raise ProcessError(f"{child} is not a child of {parent}")
        return child_proc

    def _require_child_budget(self, parent: AgentProcess) -> None:
        child_count = len([process for process in self.store.list_processes() if process.parent_pid == parent.pid])
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

from __future__ import annotations

import builtins
from collections.abc import Callable
from typing import Any, Iterable

from agent_libos.capability.manager import CapabilityManager
from agent_libos.exceptions import NotFound, ProcessError
from agent_libos.ids import new_id, utc_now
from agent_libos.memory.object_memory import ObjectMemoryManager
from agent_libos.models import (
    AgentProcess,
    CapabilityRight,
    EventType,
    ForkMode,
    MemoryView,
    MemoryViewSpec,
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


class ProcessManager:
    def __init__(
        self,
        store: SQLiteStore,
        memory: ObjectMemoryManager,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
    ):
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
        image: str = "base-agent:v0",
        goal: dict[str, Any] | str | ObjectHandle | None = None,
        capabilities: builtins.list[dict[str, Any]] | None = None,
        resource_budget: ResourceBudget | None = None,
    ) -> str:
        now = utc_now()
        pid = new_id("pid")
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
            resource_budget=resource_budget or ResourceBudget(),
            created_at=now,
            updated_at=now,
        )
        self.store.insert_process(process)
        goal_handle = self._ensure_goal(pid, goal)
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
            payload={"pid": pid, "image": image, "goal_oid": goal_handle.oid},
        )
        self.audit.record(
            actor="runtime",
            action="process.spawn",
            target=f"process:{pid}",
            output_refs=[goal_handle.oid],
            decision={"image": image},
        )
        self._run_after_spawn_hooks(pid, image)
        return pid

    def fork(
        self,
        parent: str,
        goal: dict[str, Any] | str | ObjectHandle,
        memory_view: MemoryView | MemoryViewSpec | None = None,
        capabilities: builtins.list[dict[str, Any]] | None = None,
        image: str | None = None,
        mode: ForkMode | str = ForkMode.RESTRICTED,
    ) -> str:
        parent_proc = self._get(parent)
        fork_mode = ForkMode(mode)
        if parent_proc.status in {ProcessStatus.EXITED, ProcessStatus.FAILED, ProcessStatus.KILLED}:
            raise ProcessError(f"cannot fork terminated process: {parent}")
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
                max_tool_calls=max(1, parent_proc.resource_budget.max_tool_calls // 2),
                max_child_processes=max(0, parent_proc.resource_budget.max_child_processes // 2),
                max_runtime_seconds=parent_proc.resource_budget.max_runtime_seconds,
                max_materialized_tokens=parent_proc.resource_budget.max_materialized_tokens,
            ),
            created_at=now,
            updated_at=now,
        )
        self.store.insert_process(child)
        goal_handle = self._ensure_goal(child_pid, goal)
        source_view = parent_proc.memory_view or self.memory.create_view(parent, [], mode=ViewMode.READ_ONLY)
        if isinstance(memory_view, MemoryView):
            source_view = memory_view
            spec = MemoryViewSpec(mode=self._fork_mode_to_view_mode(fork_mode))
        else:
            spec = memory_view or MemoryViewSpec(mode=self._fork_mode_to_view_mode(fork_mode))
        child_view = self.memory.fork_view(parent, child_pid, source_view, spec)
        child_view.roots.append(goal_handle)
        child.goal_oid = goal_handle.oid
        child.memory_view = child_view
        child.status = ProcessStatus.RUNNABLE
        child.updated_at = utc_now()
        self.store.update_process(child)
        self._grant_specs(child_pid, capabilities or [], issued_by=f"process.fork:{parent}")
        self.events.emit(
            EventType.PROCESS_FORKED,
            source=parent,
            target=child_pid,
            payload={"parent": parent, "child": child_pid, "mode": fork_mode.value},
        )
        self.audit.record(
            actor=parent,
            action="process.fork",
            target=f"process:{child_pid}",
            input_refs=[parent_proc.goal_oid] if parent_proc.goal_oid else [],
            output_refs=[goal_handle.oid],
            decision={"mode": fork_mode.value, "image": child.image_id},
        )
        self._run_after_spawn_hooks(child_pid, child.image_id)
        return child_pid

    def exec(
        self,
        pid: str,
        image: str,
        args: dict[str, Any] | None = None,
        preserve_memory: bool = True,
        preserve_capabilities: bool = False,
    ) -> None:
        process = self._get(pid)
        old_image = process.image_id
        if not preserve_capabilities:
            kept: builtins.list[str] = []
            for cap_id in builtins.list(process.capabilities):
                cap = self.store.get_capability(cap_id)
                if cap is None:
                    continue
                if cap.resource.startswith("object:"):
                    kept.append(cap_id)
                else:
                    self.capabilities.revoke(cap_id, revoked_by="process.exec", reason="exec capability shrink")
            process.capabilities = kept
        if not preserve_memory:
            process.memory_view = None
        process.image_id = image
        process.updated_at = utc_now()
        self.store.update_process(process)
        self.audit.record(
            actor=pid,
            action="process.exec",
            target=f"process:{pid}",
            decision={
                "old_image": old_image,
                "new_image": image,
                "args": args or {},
                "preserve_memory": preserve_memory,
                "preserve_capabilities": preserve_capabilities,
            },
        )

    def wait(self, pid: str, child: str, timeout: float | None = None) -> ProcessResult:
        parent = self._get(pid)
        child_proc = self._get(child)
        if child_proc.parent_pid != parent.pid:
            raise ProcessError(f"{child} is not a child of {pid}")
        if child_proc.status not in {ProcessStatus.EXITED, ProcessStatus.FAILED, ProcessStatus.KILLED}:
            parent.status = ProcessStatus.WAITING_EVENT
            parent.status_message = f"waiting for {child}"
            parent.updated_at = utc_now()
            self.store.update_process(parent)
            if timeout == 0:
                raise TimeoutError(f"child still running: {child}")
            raise TimeoutError(f"child still running: {child}")
        result_handle = None
        if child_proc.status_message and child_proc.status_message.startswith("result_oid:"):
            oid = child_proc.status_message.split(":", 1)[1]
            result_handle = self.capabilities.handle_for_object(
                pid,
                oid,
                {"read", "materialize", "link", "diff"},
                issued_by=f"process.wait:{child}",
            )
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

    def signal(self, target: str, signal: ProcessSignal | str, payload: dict[str, Any] | None = None) -> None:
        proc = self._get(target)
        sig = ProcessSignal(signal)
        if sig == ProcessSignal.PAUSE:
            proc.status = ProcessStatus.PAUSED
        elif sig == ProcessSignal.RESUME:
            proc.status = ProcessStatus.RUNNABLE
        elif sig in {ProcessSignal.CANCEL, ProcessSignal.TERMINATE}:
            proc.status = ProcessStatus.KILLED
        proc.status_message = (payload or {}).get("reason")
        proc.updated_at = utc_now()
        self.store.update_process(proc)
        self.events.emit(
            EventType.PROCESS_SIGNAL,
            source="runtime",
            target=target,
            payload={"signal": sig.value, "payload": payload or {}},
        )
        self.audit.record(
            actor="runtime",
            action="process.signal",
            target=f"process:{target}",
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
        self.memory.release_process_owned(pid, preserve_oids={result.oid} if result is not None else set())

    def get(self, pid: str) -> AgentProcess:
        return self._get(pid)

    def list(self) -> builtins.list[AgentProcess]:
        return self.store.list_processes()

    def _get(self, pid: str) -> AgentProcess:
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        return process

    def _ensure_goal(self, pid: str, goal: dict[str, Any] | str | ObjectHandle | None) -> ObjectHandle:
        if isinstance(goal, ObjectHandle):
            return goal
        payload = {"text": goal or "Run agent process"} if isinstance(goal, str) or goal is None else goal
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

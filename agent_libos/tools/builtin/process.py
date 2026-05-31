from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agent_libos.models import (
    AgentProcess,
    ForkMode,
    MemoryViewSpec,
    MergePolicy,
    ObjectHandle,
    ObjectMetadata,
    ObjectRight,
    ObjectType,
    ProcessSignal,
    ViewMode,
)
from agent_libos.tools.base import SyncAgentTool, ToolContext, ToolErrorCode, ToolExecutionError, ToolPolicy


class ProcessExitArgs(BaseModel):
    payload: dict[str, Any] | None = Field(default=None, description="Optional structured final result.")
    result_oid: str | None = Field(default=None, description="Existing object id to use as process result.")
    message: str | None = Field(default=None, description="Optional status message.")

    @field_validator("payload", mode="before")
    @classmethod
    def parse_json_payload(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return {"content": value}
            if isinstance(decoded, dict):
                return decoded
            return {"value": decoded}
        return value


class ProcessExitOutput(BaseModel):
    status: str
    result_oid: str | None = None


class ForkChildProcessArgs(BaseModel):
    goal: str | dict[str, Any] = Field(description="Goal for the child AgentProcess.")
    mode: str = Field(default=ForkMode.WORKER.value, description="Fork mode: copy, restricted, speculative, or worker.")
    image: str | None = Field(
        default=None,
        description="Optional image id. MVP tool only allows the current process image.",
    )
    include_parent_roots: bool = Field(
        default=True,
        description="Whether the child view includes the parent's current MemoryView roots.",
    )
    root_oids: list[str] | None = Field(
        default=None,
        description="Optional explicit Object ids to expose to the child instead of all parent roots.",
    )


class ForkChildProcessOutput(BaseModel):
    child_pid: str
    parent_pid: str
    image: str
    mode: str
    status: str
    goal_oid: str | None


class WaitChildProcessArgs(BaseModel):
    child_pid: str = Field(description="Direct child process id to wait for.")
    block: bool = Field(default=True, description="If false, return ready=false when the child is still running.")


class WaitChildProcessOutput(BaseModel):
    child_pid: str
    status: str
    ready: bool
    result_oid: str | None = None
    message: str | None = None


class ChildProcessInfo(BaseModel):
    pid: str
    image: str
    status: str
    goal_oid: str | None
    result_oid: str | None = None
    status_message: str | None = None


class ListChildProcessesArgs(BaseModel):
    include_terminal: bool = Field(default=True, description="Whether exited/failed/killed children are included.")


class ListChildProcessesOutput(BaseModel):
    children: list[ChildProcessInfo]


class SignalChildProcessArgs(BaseModel):
    child_pid: str = Field(description="Direct child process id to signal.")
    signal: str = Field(description="Signal to send: pause, resume, cancel, or terminate.")
    reason: str | None = Field(default=None, description="Optional reason stored in the child status message.")


class SignalChildProcessOutput(BaseModel):
    child_pid: str
    signal: str
    status: str


class MergeChildMemoryArgs(BaseModel):
    child_pid: str = Field(description="Direct child process id whose memory should be merged.")
    include_child_created: bool = Field(default=True, description="Include objects created by the child.")


class MergeChildMemoryOutput(BaseModel):
    child_pid: str
    merged_oids: list[str]
    skipped_oids: list[str]


class ProcessExitTool(SyncAgentTool[ProcessExitArgs]):
    name = "process_exit"
    description = (
        "Exit the current Agent Process with an optional final result. "
        "This is a Skills/Tools Layer wrapper over process lifecycle primitives."
    )
    args_schema = ProcessExitArgs
    output_schema = ProcessExitOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=False, idempotent=True, timeout_s=5.0)
    tags = ["process", "lifecycle"]

    def run(self, args: ProcessExitArgs, ctx: ToolContext) -> ProcessExitOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        result_handle: ObjectHandle | None = None
        if args.result_oid:
            result_handle = runtime.capability.handle_for_object(
                ctx.pid,
                args.result_oid,
                {"read", "materialize", "link", "diff"},
                issued_by="process_exit_tool",
            )
        elif args.payload is not None:
            result_handle = runtime.memory.create_object(
                pid=ctx.pid,
                object_type=ObjectType.SUMMARY,
                payload=args.payload,
                metadata=ObjectMetadata(title="Process final result", tags=["final"]),
            )
        runtime.process.exit(ctx.pid, result=result_handle, message=args.message)
        result_oid = result_handle.oid if result_handle is not None else None
        return ProcessExitOutput(status="exited", result_oid=result_oid)


class ForkChildProcessTool(SyncAgentTool[ForkChildProcessArgs]):
    name = "fork_child_process"
    description = (
        "Fork a direct child AgentProcess with an attenuated MemoryView. "
        "This creates an Agent libOS child process, not a host OS process."
    )
    args_schema = ForkChildProcessArgs
    output_schema = ForkChildProcessOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=True, idempotent=False, timeout_s=5.0)
    tags = ["process", "child", "fork"]

    def run(self, args: ForkChildProcessArgs, ctx: ToolContext) -> ForkChildProcessOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        parent = runtime.process.get(ctx.pid)
        image = args.image or parent.image_id
        if image != parent.image_id:
            raise ToolExecutionError(
                "Forking into a different image is not exposed to processes yet.",
                code=ToolErrorCode.PERMISSION_DENIED,
                details={"requested_image": image, "parent_image": parent.image_id},
            )
        try:
            fork_mode = ForkMode(args.mode)
        except ValueError as exc:
            raise ToolExecutionError(
                "Invalid fork mode.",
                code=ToolErrorCode.VALIDATION_ERROR,
                details={"mode": args.mode, "allowed": [mode.value for mode in ForkMode]},
            ) from exc
        roots = self._selected_roots(runtime, ctx.pid, args.root_oids)
        view_spec = MemoryViewSpec(
            roots=roots,
            mode=_view_mode_for_fork(fork_mode),
            include_parent_roots=args.include_parent_roots,
        )
        child_pid = runtime.process.fork(
            parent=ctx.pid,
            goal=args.goal,
            memory_view=view_spec,
            image=image,
            mode=fork_mode,
        )
        child = runtime.process.get(child_pid)
        return ForkChildProcessOutput(
            child_pid=child.pid,
            parent_pid=ctx.pid,
            image=child.image_id,
            mode=fork_mode.value,
            status=child.status.value,
            goal_oid=child.goal_oid,
        )

    def _selected_roots(self, runtime: Any, pid: str, root_oids: list[str] | None) -> list[ObjectHandle] | None:
        if root_oids is None:
            return None
        process = runtime.process.get(pid)
        visible = {handle.oid: handle for handle in (process.memory_view.roots if process.memory_view else [])}
        roots: list[ObjectHandle] = []
        for oid in root_oids:
            if oid in visible:
                roots.append(visible[oid])
                continue
            runtime.capability.require(pid, f"object:{oid}", ObjectRight.READ)
            roots.append(
                runtime.capability.handle_for_object(
                    pid,
                    oid,
                    {"read", "materialize", "diff"},
                    issued_by="fork_child_process_tool",
                )
            )
        return roots


class WaitChildProcessTool(SyncAgentTool[WaitChildProcessArgs]):
    name = "wait_child_process"
    description = (
        "Wait for a direct child AgentProcess to exit, fail, or be killed. "
        "If the child is still running and block=true, the current process is suspended and the same wait resumes later."
    )
    args_schema = WaitChildProcessArgs
    output_schema = WaitChildProcessOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=True, idempotent=False, timeout_s=5.0)
    tags = ["process", "child", "wait"]

    def run(self, args: WaitChildProcessArgs, ctx: ToolContext) -> WaitChildProcessOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        try:
            result = runtime.process.wait(
                ctx.pid,
                args.child_pid,
                timeout=None if args.block else 0,
            )
        except TimeoutError:
            child = runtime.process.get(args.child_pid)
            return WaitChildProcessOutput(
                child_pid=args.child_pid,
                status=child.status.value,
                ready=False,
                message=child.status_message,
            )
        return WaitChildProcessOutput(
            child_pid=result.pid,
            status=result.status.value,
            ready=True,
            result_oid=result.result.oid if result.result is not None else None,
            message=result.message,
        )


class ListChildProcessesTool(SyncAgentTool[ListChildProcessesArgs]):
    name = "list_child_processes"
    description = "List direct child AgentProcesses owned by the current process."
    args_schema = ListChildProcessesArgs
    output_schema = ListChildProcessesOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=False, idempotent=True, timeout_s=5.0)
    tags = ["process", "child", "inspect"]

    def run(self, args: ListChildProcessesArgs, ctx: ToolContext) -> ListChildProcessesOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        return ListChildProcessesOutput(
            children=[_child_info(child) for child in runtime.process.list_children(ctx.pid, args.include_terminal)]
        )


class SignalChildProcessTool(SyncAgentTool[SignalChildProcessArgs]):
    name = "signal_child_process"
    description = "Pause, resume, cancel, or terminate a direct child AgentProcess."
    args_schema = SignalChildProcessArgs
    output_schema = SignalChildProcessOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=True, idempotent=False, timeout_s=5.0)
    tags = ["process", "child", "signal"]

    def run(self, args: SignalChildProcessArgs, ctx: ToolContext) -> SignalChildProcessOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        try:
            signal = ProcessSignal(args.signal)
        except ValueError as exc:
            raise ToolExecutionError(
                "Invalid process signal.",
                code=ToolErrorCode.VALIDATION_ERROR,
                details={"signal": args.signal, "allowed": ["pause", "resume", "cancel", "terminate"]},
            ) from exc
        if signal not in {ProcessSignal.PAUSE, ProcessSignal.RESUME, ProcessSignal.CANCEL, ProcessSignal.TERMINATE}:
            raise ToolExecutionError(
                "Signal is not exposed through this tool.",
                code=ToolErrorCode.PERMISSION_DENIED,
                details={"signal": signal.value},
            )
        child = runtime.process.signal_child(ctx.pid, args.child_pid, signal, reason=args.reason)
        return SignalChildProcessOutput(child_pid=child.pid, signal=signal.value, status=child.status.value)


class MergeChildMemoryTool(SyncAgentTool[MergeChildMemoryArgs]):
    name = "merge_child_memory"
    description = "Merge result-visible Object Memory from an exited direct child into the parent process view."
    args_schema = MergeChildMemoryArgs
    output_schema = MergeChildMemoryOutput
    version = "1.0.0"
    policy = ToolPolicy(side_effects=True, idempotent=False, timeout_s=5.0)
    tags = ["process", "child", "memory"]

    def run(self, args: MergeChildMemoryArgs, ctx: ToolContext) -> MergeChildMemoryOutput:
        runtime = ctx.runtime
        if runtime is None:
            raise ToolExecutionError("Runtime is unavailable.", code=ToolErrorCode.EXECUTION_ERROR)
        result = runtime.process.merge_child_memory(
            ctx.pid,
            args.child_pid,
            policy=MergePolicy(include_child_created=args.include_child_created),
        )
        return MergeChildMemoryOutput(
            child_pid=args.child_pid,
            merged_oids=result.merged_oids,
            skipped_oids=result.skipped_oids,
        )


def _view_mode_for_fork(mode: ForkMode) -> ViewMode:
    if mode == ForkMode.COPY:
        return ViewMode.COPY_ON_WRITE
    if mode == ForkMode.SPECULATIVE:
        return ViewMode.EPHEMERAL
    return ViewMode.READ_ONLY


def _child_info(child: AgentProcess) -> ChildProcessInfo:
    result_oid = None
    if child.status_message and child.status_message.startswith("result_oid:"):
        result_oid = child.status_message.split(":", 1)[1]
    return ChildProcessInfo(
        pid=child.pid,
        image=child.image_id,
        status=child.status.value,
        goal_oid=child.goal_oid,
        result_oid=result_oid,
        status_message=child.status_message,
    )

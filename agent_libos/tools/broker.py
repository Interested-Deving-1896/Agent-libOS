from __future__ import annotations

import asyncio
import builtins
import hashlib
import inspect
import time
from pathlib import Path
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models.exceptions import HumanApprovalRequired, NotFound, ProcessMessageWaitRequired, ProcessWaitRequired, ResourceLimitExceeded, ValidationError
from agent_libos.human.manager import HumanObjectManager
from agent_libos.utils.ids import new_id, utc_now
from agent_libos.memory.object_memory import ObjectMemoryManager
from agent_libos.models import (
    EventType,
    ObjectMetadata,
    ObjectType,
    ProcessStatus,
    ResourceUsage,
    ToolCallResult,
    ToolCandidate,
    ToolCandidateStatus,
    ToolHandle,
    ToolSpec,
    ValidationResult,
)
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.runtime.syscalls import LibOSSyscallSession
from agent_libos.storage import SQLiteStore
from agent_libos.substrate import CommandMetrics, SubprocessLimitExceeded, SubprocessLimits, SubprocessTimeoutExpired
from agent_libos.tools.base import BaseAgentTool, ToolContext
from agent_libos.tools.observability import ensure_json_size, sanitize_for_observability
from agent_libos.tools.sandbox import DenoTypescriptSandbox, SandboxBackend, SandboxExecutionResult

_TOOL_DEFAULTS = DEFAULT_CONFIG.tools
_TERMINAL_PROCESS_STATUSES = {ProcessStatus.EXITED, ProcessStatus.FAILED, ProcessStatus.KILLED}


class ToolBroker:
    """Registry and dispatch boundary for model-facing tools."""

    def __init__(
        self,
        store: SQLiteStore,
        memory: ObjectMemoryManager,
        capabilities: CapabilityManager,
        human: HumanObjectManager,
        audit: AuditManager,
        events: EventBus,
        sandbox: SandboxBackend | None = None,
        workspace_root: str | Path | None = None,
        config: AgentLibOSConfig | None = None,
        resources: Any | None = None,
    ):
        self.config = config or DEFAULT_CONFIG
        self.store = store
        self.memory = memory
        self.capabilities = capabilities
        self.human = human
        self.audit = audit
        self.events = events
        self.resources = resources
        self.sandbox = sandbox or DenoTypescriptSandbox(
            deno_executable=self.config.tools.deno_executable,
            default_timeout_s=self.config.tools.deno_timeout_s,
            max_rpc_calls=self.config.tools.deno_max_rpc_calls,
            max_stdout_bytes=self.config.tools.deno_max_stdout_bytes,
            max_stderr_bytes=self.config.tools.deno_max_stderr_bytes,
            jsr_allowlist=self.config.tools.deno_jsr_allowlist,
        )
        self.workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self._tools: dict[str, BaseAgentTool] = {}
        self._tool_ids_by_name: dict[str, str] = {}
        self._handles: dict[str, ToolHandle] = {}
        self._jit_sources: dict[str, str] = {}

    def register_tool(
        self,
        tool: BaseAgentTool,
        registered_by: str = "runtime",
        scope: str = "static",
        ephemeral: bool = False,
    ) -> ToolHandle:
        spec = tool.spec()
        if spec.name in self._tool_ids_by_name:
            raise ValueError(f"tool already registered: {spec.name}")
        tool_id = new_id("tool") if ephemeral else _stable_static_tool_id(
            spec.name,
            digest_chars=self.config.tools.static_tool_id_digest_chars,
        )
        handle = ToolHandle(tool_id=tool_id, name=spec.name, capability_id=None, scope=scope)
        self._tools[tool_id] = tool
        self._tool_ids_by_name[spec.name] = tool_id
        self._handles[tool_id] = handle
        existing = next((row for row in self.store.list_tools() if row["tool_id"] == tool_id), None)
        if existing is not None and existing["name"] != spec.name:
            raise ValueError(f"tool id collision: {tool_id}")
        if existing is None:
            self.store.insert_tool(handle, spec, registered_by=registered_by, created_at=utc_now(), ephemeral=ephemeral)
        self.audit.record(
            actor=registered_by,
            action="tool.register",
            target=f"tool:{tool_id}",
            decision={
                "name": spec.name,
                "version": spec.version,
                "policy": spec.policy,
                "tags": spec.tags,
            },
        )
        return handle

    def configure_process_tools(
        self,
        pid: str,
        tools: builtins.list[ToolHandle | str],
        assigned_by: str = "tool_broker",
    ) -> dict[str, str]:
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        table: dict[str, str] = {}
        for tool in tools:
            handle = self.resolve(tool)
            table[handle.name] = handle.tool_id
        process.tool_table = table
        process.updated_at = utc_now()
        self.store.update_process(process)
        self.audit.record(
            actor=assigned_by,
            action="process.tools.configure",
            target=f"process:{pid}",
            decision={"tools": sorted(table)},
        )
        return table

    def grant_execute(self, pid: str, tool: ToolHandle | str, issued_by: str = "tool_broker") -> str:
        handle = self.resolve(tool, pid=pid)
        if not self._process_has_tool(pid, handle):
            raise ValidationError(
                "tool execute capabilities are not a resource authority; configure process tools at creation time"
            )
        self.audit.record(
            actor=issued_by,
            action="tool.execute_grant_ignored",
            target=f"tool:{handle.tool_id}",
            decision={
                "tool": handle.name,
                "reason": "tool calls are allowed by process tool table, not execute capability",
            },
        )
        return handle.tool_id

    def propose(
        self,
        pid: str,
        spec: ToolSpec | dict[str, Any],
        source_code: str,
        tests: builtins.list[dict[str, Any]] | None = None,
        requested_capabilities: builtins.list[dict[str, Any]] | None = None,
    ) -> str:
        tool_spec = spec if isinstance(spec, ToolSpec) else ToolSpec(**spec)
        self._validate_jit_source_and_tests(source_code, tests or [])
        now = utc_now()
        candidate = ToolCandidate(
            candidate_id=new_id("tcand"),
            pid=pid,
            spec=tool_spec,
            source_code=source_code,
            tests=tests or [],
            requested_capabilities=requested_capabilities or [],
            status=ToolCandidateStatus.PROPOSED,
            validation=None,
            created_at=now,
            updated_at=now,
        )
        self.store.insert_tool_candidate(candidate)
        candidate_obj = self.memory.create_object(
            pid=pid,
            object_type=ObjectType.TOOL_CANDIDATE,
            payload={
                "candidate_id": candidate.candidate_id,
                "language": self.sandbox.language,
                "spec": {
                    "name": tool_spec.name,
                    "description": tool_spec.description,
                    "input_schema": tool_spec.input_schema,
                    "output_schema": tool_spec.output_schema,
                    "side_effects": tool_spec.side_effects,
                },
                "tests": candidate.tests,
                "requested_capabilities": candidate.requested_capabilities,
            },
            metadata=ObjectMetadata(title=f"Tool candidate: {tool_spec.name}", tags=["tool", "candidate"]),
            immutable=True,
        )
        self.audit.record(
            actor=pid,
            action="tool.propose",
            target=f"tool_candidate:{candidate.candidate_id}",
            output_refs=[candidate_obj.oid],
            decision={"name": tool_spec.name},
        )
        return candidate.candidate_id

    def validate(self, candidate_id: str, *, pid: str | None = None) -> ValidationResult:
        candidate = self._get_candidate(candidate_id)
        owner_pid = pid or candidate.pid
        self._require_candidate_owner(candidate, owner_pid)
        limits = self._subprocess_limits(owner_pid)
        try:
            result = self._run_candidate_tests(candidate, limits)
        except SubprocessLimitExceeded as exc:
            self._charge_subprocess_metrics(
                owner_pid,
                exc.metrics,
                source="tool.validate.deno",
                context={"candidate_id": candidate_id, "tool": candidate.spec.name},
            )
            raise
        except SubprocessTimeoutExpired as exc:
            self._charge_subprocess_metrics(
                owner_pid,
                exc.metrics,
                source="tool.validate.deno",
                context={"candidate_id": candidate_id, "tool": candidate.spec.name},
            )
            raise
        metrics = self._metrics_from_validation(result.metadata.get("metrics"))
        self._charge_subprocess_metrics(
            owner_pid,
            metrics,
            source="tool.validate.deno",
            context={"candidate_id": candidate_id, "tool": candidate.spec.name},
        )
        errors = list(result.errors)
        warnings = list(result.warnings)
        if candidate.requested_capabilities:
            errors.append("Deno/TypeScript JIT tools cannot request external capabilities")
        validation = ValidationResult(
            ok=not errors and result.ok,
            errors=errors,
            warnings=warnings,
            logs=result.logs,
            metadata=result.metadata,
        )
        metadata = self.sandbox.metadata_for_source(candidate.source_code)
        candidate.validation = {
            "ok": validation.ok,
            "errors": validation.errors,
            "warnings": validation.warnings,
            "logs": validation.logs,
            "metrics": validation.metadata.get("metrics"),
            **metadata,
        }
        candidate.status = ToolCandidateStatus.VALIDATED if validation.ok else ToolCandidateStatus.REJECTED
        candidate.updated_at = utc_now()
        self.store.update_tool_candidate(candidate)
        self.audit.record(
            actor="tool_broker",
            action="tool.validate",
            target=f"tool_candidate:{candidate_id}",
            decision=candidate.validation,
        )
        return validation

    def register(
        self,
        pid: str,
        candidate_id: str,
        approver: str = "policy:local",
        scope: str = "ephemeral_process",
    ) -> ToolHandle:
        candidate = self._get_candidate(candidate_id)
        self._require_candidate_owner(candidate, pid)
        if candidate.status == ToolCandidateStatus.REGISTERED:
            raise ValidationError(f"tool candidate is already registered: {candidate_id}")
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        if candidate.spec.name in process.tool_table:
            raise ValidationError(f"process already has a tool named: {candidate.spec.name}")
        if self._name_collides_with_static_tool(candidate.spec.name):
            raise ValidationError(f"tool name already exists: {candidate.spec.name}")
        if candidate.status != ToolCandidateStatus.VALIDATED:
            validation = self.validate(candidate_id, pid=pid)
            if not validation.ok:
                raise ValidationError("; ".join(validation.errors))
            candidate = self._get_candidate(candidate_id)
        tool_id = new_id("tool")
        handle = ToolHandle(tool_id=tool_id, name=candidate.spec.name, capability_id=None, scope=scope)
        self._jit_sources[tool_id] = candidate.source_code
        self._handles[tool_id] = handle
        # JIT tool names are process-local through AgentProcess.tool_table. Do
        # not add them to the global name index, otherwise later pid-less
        # resolve(name) calls can turn one process' JIT into a globally
        # referenceable tool.
        self.store.insert_tool(handle, candidate.spec, registered_by=approver, created_at=utc_now(), ephemeral=True)
        candidate.status = ToolCandidateStatus.REGISTERED
        candidate.updated_at = utc_now()
        self.store.update_tool_candidate(candidate)
        process.tool_table[candidate.spec.name] = tool_id
        process.updated_at = utc_now()
        self.store.update_process(process)
        self.audit.record(
            actor=approver,
            action="tool.register",
            target=f"tool:{tool_id}",
            decision={"candidate_id": candidate_id, "scope": scope},
        )
        return handle

    def call(self, pid: str, tool: ToolHandle | str, args: dict[str, Any]) -> ToolCallResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.acall(pid, tool, args))
        raise RuntimeError("Cannot call ToolBroker.call() inside a running event loop. Use await acall(...).")

    async def acall(self, pid: str, tool: ToolHandle | str, args: dict[str, Any]) -> ToolCallResult:
        handle = self.resolve(tool, pid=pid)
        resource = f"tool:{handle.tool_id}"
        terminal_error = self._terminal_process_error(pid)
        if terminal_error is not None:
            call_id = new_id("tcall")
            self.events.emit(
                EventType.TOOL_FAILED,
                source=resource,
                target=pid,
                payload={"call_id": call_id, "error": terminal_error, "policy_decision": "deny"},
            )
            self.audit.record(
                actor=pid,
                action="tool.call",
                target=resource,
                decision={
                    "ok": False,
                    "tool": handle.name,
                    "policy_decision": "deny",
                    "policy_reason": "terminal_process",
                    "error": terminal_error,
                },
            )
            return ToolCallResult(call_id=call_id, tool_id=handle.tool_id, result_handle=None, payload=None, ok=False, error=terminal_error)
        if not self._process_has_tool(pid, handle):
            # The process tool table gates only LLM-facing tool visibility.
            # Host resources are checked by the primitive each tool calls into.
            call_id = new_id("tcall")
            error = f"tool is not in process tool table: {handle.name}"
            self.events.emit(
                EventType.TOOL_FAILED,
                source=resource,
                target=pid,
                payload={"call_id": call_id, "error": error, "policy_decision": "deny"},
            )
            self.audit.record(
                actor=pid,
                action="tool.call",
                target=resource,
                decision={
                    "ok": False,
                    "tool": handle.name,
                    "policy_decision": "deny",
                    "policy_reason": "tool_not_in_process_table",
                },
            )
            return ToolCallResult(call_id=call_id, tool_id=handle.tool_id, result_handle=None, payload=None, ok=False, error=error)

        call_id = new_id("tcall")
        try:
            self._charge_tool_call(pid, handle, call_id)
        except ResourceLimitExceeded as exc:
            error = str(exc)
            self.events.emit(
                EventType.TOOL_FAILED,
                source=resource,
                target=pid,
                payload={"call_id": call_id, "error": error, "policy_decision": "resource_limit"},
            )
            self.audit.record(
                actor=pid,
                action="tool.call",
                target=resource,
                decision={
                    "ok": False,
                    "tool": handle.name,
                    "policy_decision": "resource_limit",
                    "error": error,
                },
            )
            return ToolCallResult(call_id=call_id, tool_id=handle.tool_id, result_handle=None, payload=None, ok=False, error=error)
        self.events.emit(
            EventType.TOOL_CALLED,
            source=pid,
            target=resource,
            payload={"call_id": call_id, "args": sanitize_for_observability(args)},
        )
        started_at = time.perf_counter()
        try:
            jit_session: LibOSSyscallSession | None = None
            if handle.tool_id in self._tools:
                tool_result = await self._tools[handle.tool_id].ainvoke(args, self._context(pid, handle, call_id))
                if not tool_result.ok:
                    error_message = tool_result.error.message if tool_result.error else tool_result.content
                    self.events.emit(
                        EventType.TOOL_FAILED,
                        source=resource,
                        target=pid,
                        payload={
                            "call_id": call_id,
                            "error": error_message,
                            "tool_result": sanitize_for_observability(tool_result.model_dump(mode="json")),
                        },
                    )
                    self.audit.record(
                        actor=pid,
                        action="tool.call",
                        target=resource,
                        decision={
                            "ok": False,
                            "tool": handle.name,
                            "policy_decision": "allow",
                            "tool_result": sanitize_for_observability(tool_result.model_dump(mode="json")),
                            "tool_wall_seconds": self._elapsed(started_at),
                        },
                    )
                    return ToolCallResult(
                        call_id=call_id,
                        tool_id=handle.tool_id,
                        result_handle=None,
                        payload=tool_result.model_dump(mode="json"),
                        ok=False,
                        error=error_message,
                    )
                payload = tool_result.data
                result_payload = {
                    "tool_id": handle.tool_id,
                    "tool_name": handle.name,
                    "result": payload,
                    "content": tool_result.content,
                    "artifacts": [artifact.model_dump(mode="json") for artifact in tool_result.artifacts],
                    "metadata": tool_result.metadata,
                }
            elif handle.tool_id in self._jit_sources:
                runtime = getattr(self, "runtime", None)
                if runtime is None:
                    raise RuntimeError("Runtime is unavailable for Deno JIT syscall execution.")
                jit_session = LibOSSyscallSession(runtime, pid, config=self.config)
                deno_result = await self._run_sandbox_source(
                    self._jit_sources[handle.tool_id],
                    args,
                    pid=pid,
                    syscall_handler=jit_session.handle,
                )
                if isinstance(deno_result, SandboxExecutionResult):
                    payload = deno_result.value
                    self._charge_subprocess_metrics(
                        pid,
                        deno_result.metrics,
                        source="tool.deno",
                        context={"tool": handle.name, "tool_id": handle.tool_id},
                    )
                else:
                    payload = deno_result
                result_payload = {"tool_id": handle.tool_id, "tool_name": handle.name, "result": payload}
            else:
                raise NotFound(f"tool implementation not loaded: {handle.tool_id}")
        except SubprocessLimitExceeded as exc:
            self._handle_subprocess_limit(pid, handle, resource, call_id, exc)
            return ToolCallResult(
                call_id=call_id,
                tool_id=handle.tool_id,
                result_handle=None,
                payload=None,
                ok=False,
                error=str(exc),
            )
        except SubprocessTimeoutExpired as exc:
            error = self._handle_subprocess_timeout(pid, handle, resource, call_id, exc)
            return ToolCallResult(
                call_id=call_id,
                tool_id=handle.tool_id,
                result_handle=None,
                payload=None,
                ok=False,
                error=error,
            )
        except HumanApprovalRequired as exc:
            # Do not convert this into a ToolCallResult: the LLM quantum has not
            # completed and must be resumed after the human decision.
            self.audit.record(
                actor=pid,
                action="tool.call_waiting_human",
                target=resource,
                decision={
                    "ok": False,
                    "tool": handle.name,
                    "policy_decision": "require_human_approval",
                    "request_id": exc.request_id,
                    "tool_wall_seconds": self._elapsed(started_at),
                },
            )
            raise
        except ProcessWaitRequired as exc:
            self.audit.record(
                actor=pid,
                action="tool.call_waiting_process",
                target=resource,
                decision={
                    "ok": False,
                    "tool": handle.name,
                    "policy_decision": "wait_for_child",
                    "child_pid": exc.child_pid,
                    "tool_wall_seconds": self._elapsed(started_at),
                },
            )
            raise
        except ProcessMessageWaitRequired as exc:
            self.audit.record(
                actor=pid,
                action="tool.call_waiting_message",
                target=resource,
                decision={
                    "ok": False,
                    "tool": handle.name,
                    "policy_decision": "wait_for_process_message",
                    "recipient_pid": exc.recipient_pid,
                    "filters": exc.filters,
                    "tool_wall_seconds": self._elapsed(started_at),
                },
            )
            raise
        except Exception as exc:
            self.events.emit(
                EventType.TOOL_FAILED,
                source=resource,
                target=pid,
                payload={"call_id": call_id, "error": sanitize_for_observability(str(exc))},
            )
            self.audit.record(
                actor=pid,
                action="tool.call",
                target=resource,
                decision={
                    "ok": False,
                    "tool": handle.name,
                    "policy_decision": "allow",
                    "error": sanitize_for_observability(str(exc)),
                    "tool_wall_seconds": self._elapsed(started_at),
                },
            )
            return ToolCallResult(call_id=call_id, tool_id=handle.tool_id, result_handle=None, payload=None, ok=False, error=str(exc))

        try:
            ensure_json_size(
                result_payload,
                self.config.tools.tool_result_payload_hard_limit_bytes,
                "tool result payload",
            )
        except ValidationError as exc:
            error = str(exc)
            self.events.emit(
                EventType.TOOL_FAILED,
                source=resource,
                target=pid,
                payload={"call_id": call_id, "error": error, "policy_decision": "validation_error"},
            )
            self.audit.record(
                actor=pid,
                action="tool.call",
                target=resource,
                decision={
                    "ok": False,
                    "tool": handle.name,
                    "policy_decision": "validation_error",
                    "error": error,
                    "result": sanitize_for_observability(result_payload),
                    "tool_wall_seconds": self._elapsed(started_at),
                },
            )
            return ToolCallResult(call_id=call_id, tool_id=handle.tool_id, result_handle=None, payload=None, ok=False, error=error)

        result_handle = self.memory.create_object(
            pid=pid,
            object_type=ObjectType.TOOL_RESULT,
            payload=result_payload,
            metadata=ObjectMetadata(title=f"Tool result: {handle.name}", tags=["tool_result", handle.name]),
            immutable=True,
        )
        self.events.emit(
            EventType.TOOL_COMPLETED,
            source=resource,
            target=pid,
            payload={"call_id": call_id, "result_oid": result_handle.oid},
        )
        self.audit.record(
            actor=pid,
            action="tool.call",
            target=resource,
            output_refs=[result_handle.oid],
            decision={
                "ok": True,
                "tool": handle.name,
                "policy_decision": "allow",
                "tool_wall_seconds": self._elapsed(started_at),
            },
        )
        if jit_session is not None:
            await jit_session.apply_deferred_lifecycle(result_handle)
        return ToolCallResult(
            call_id=call_id,
            tool_id=handle.tool_id,
            result_handle=result_handle,
            payload=payload,
            ok=True,
        )

    def _charge_tool_call(self, pid: str, handle: ToolHandle, call_id: str) -> None:
        if self.resources is None:
            return
        self.resources.charge(
            pid,
            ResourceUsage(tool_calls=1),
            source="tool.call",
            context={"tool": handle.name, "tool_id": handle.tool_id, "call_id": call_id},
            allow_overage=False,
            kill_on_exceed=False,
        )

    async def _run_sandbox_source(
        self,
        source_code: str,
        args: dict[str, Any],
        *,
        pid: str,
        syscall_handler: Any,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "pid": pid,
            "syscall_handler": syscall_handler,
            "limits": self._subprocess_limits(pid),
            "return_metrics": True,
        }
        if kwargs["limits"] is not None:
            self._require_sandbox_resource_controls()
        supported = self._supported_sandbox_kwargs()
        selected_kwargs = {key: value for key, value in kwargs.items() if key in supported}
        if kwargs["limits"] is not None and "limits" not in selected_kwargs:
            raise ValidationError("sandbox backend must accept SubprocessLimits when resource limits are configured")
        if kwargs["limits"] is not None and "return_metrics" not in selected_kwargs:
            raise ValidationError("sandbox backend must return subprocess metrics")
        return await self.sandbox.arun_source(source_code, args, **selected_kwargs)

    def _supported_sandbox_kwargs(self) -> set[str]:
        signature = inspect.signature(self.sandbox.arun_source)
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
            return {"pid", "syscall_handler", "timeout", "limits", "return_metrics"}
        return set(signature.parameters)

    def _supported_run_tests_kwargs(self) -> set[str]:
        signature = inspect.signature(self.sandbox.run_tests)
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
            return {"timeout", "limits", "return_metrics"}
        return set(signature.parameters)

    def _require_sandbox_resource_controls(self) -> None:
        supported = self._supported_sandbox_kwargs()
        if "limits" not in supported:
            raise ValidationError("sandbox backend must accept SubprocessLimits when resource limits are configured")
        if "return_metrics" not in supported:
            raise ValidationError("sandbox backend must return subprocess metrics")

    def _require_sandbox_validation_resource_controls(self) -> None:
        supported = self._supported_run_tests_kwargs()
        if "limits" not in supported:
            raise ValidationError("sandbox backend must accept SubprocessLimits when validating with resource limits")
        if "return_metrics" not in supported:
            raise ValidationError("sandbox backend must return validation subprocess metrics")

    def _run_candidate_tests(
        self,
        candidate: ToolCandidate,
        limits: SubprocessLimits | None,
    ) -> ValidationResult:
        kwargs: dict[str, Any] = {
            "timeout": self.config.tools.jit_validation_timeout_s,
            "limits": limits,
            "return_metrics": True,
        }
        if limits is not None:
            self._require_sandbox_validation_resource_controls()
        supported = self._supported_run_tests_kwargs()
        selected_kwargs = {key: value for key, value in kwargs.items() if key in supported}
        if limits is not None and "limits" not in selected_kwargs:
            raise ValidationError("sandbox backend must accept SubprocessLimits when validating with resource limits")
        if limits is not None and "return_metrics" not in selected_kwargs:
            raise ValidationError("sandbox backend must return validation subprocess metrics")
        return self.sandbox.run_tests(candidate.source_code, candidate.tests, **selected_kwargs)

    def _validate_jit_source_and_tests(self, source_code: str, tests: list[dict[str, Any]]) -> None:
        if len(source_code) > self.config.tools.jit_source_max_chars:
            raise ValidationError(
                f"JIT source exceeds {self.config.tools.jit_source_max_chars} chars"
            )
        if len(tests) > self.config.tools.jit_tests_max_count:
            raise ValidationError(
                f"JIT tests exceed {self.config.tools.jit_tests_max_count} cases"
            )
        for index, test in enumerate(tests, start=1):
            ensure_json_size(test, self.config.tools.jit_test_case_max_bytes, f"JIT test {index}")

    def _subprocess_limits(self, pid: str) -> SubprocessLimits | None:
        if self.resources is None:
            return None
        wall = self.resources.remaining_cumulative(
            pid,
            "max_subprocess_wall_seconds",
            "subprocess_wall_seconds",
        )
        cpu = self.resources.remaining_cumulative(
            pid,
            "max_subprocess_cpu_seconds",
            "subprocess_cpu_seconds",
        )
        memory = self.resources.peak_limit(pid, "max_subprocess_memory_bytes")
        if wall is None and cpu is None and memory is None:
            return None
        return SubprocessLimits(wall_seconds=wall, cpu_seconds=cpu, memory_bytes=memory)

    def _charge_subprocess_metrics(
        self,
        pid: str,
        metrics: CommandMetrics | None,
        *,
        source: str,
        context: dict[str, Any],
    ) -> None:
        if self.resources is None or metrics is None:
            return
        self.resources.charge(
            pid,
            ResourceUsage(
                subprocess_wall_seconds=max(0.0, metrics.wall_seconds),
                subprocess_cpu_seconds=max(0.0, metrics.cpu_seconds),
                subprocess_peak_memory_bytes=max(0, metrics.peak_memory_bytes),
            ),
            source=source,
            context={**context, "metrics": self._metrics_json(metrics)},
            allow_overage=True,
            kill_on_exceed=True,
        )

    def _handle_subprocess_limit(
        self,
        pid: str,
        handle: ToolHandle,
        resource: str,
        call_id: str,
        exc: SubprocessLimitExceeded,
    ) -> None:
        charge_error: ResourceLimitExceeded | None = None
        try:
            self._charge_subprocess_metrics(
                pid,
                exc.metrics,
                source="tool.deno",
                context={"tool": handle.name, "tool_id": handle.tool_id},
            )
        except ResourceLimitExceeded as resource_exc:
            charge_error = resource_exc
        reason = str(charge_error or exc)
        if self.resources is not None:
            self.resources.kill_if_exceeded(
                pid,
                reason=reason,
                limit={"kind": exc.metrics.limit_kind, "metrics": self._metrics_json(exc.metrics)},
            )
        self.events.emit(
            EventType.TOOL_FAILED,
            source=resource,
            target=pid,
            payload={"call_id": call_id, "error": reason, "policy_decision": "resource_limit"},
        )
        self.audit.record(
            actor=pid,
            action="tool.call",
            target=resource,
            decision={
                "ok": False,
                "tool": handle.name,
                "policy_decision": "resource_limit",
                "error": reason,
                "metrics": self._metrics_json(exc.metrics),
            },
        )

    def _handle_subprocess_timeout(
        self,
        pid: str,
        handle: ToolHandle,
        resource: str,
        call_id: str,
        exc: SubprocessTimeoutExpired,
    ) -> str:
        charge_error: ResourceLimitExceeded | None = None
        try:
            self._charge_subprocess_metrics(
                pid,
                exc.metrics,
                source="tool.deno",
                context={"tool": handle.name, "tool_id": handle.tool_id},
            )
        except ResourceLimitExceeded as resource_exc:
            charge_error = resource_exc
        if charge_error is not None:
            reason = str(charge_error)
            if self.resources is not None:
                self.resources.kill_if_exceeded(
                    pid,
                    reason=reason,
                    limit={"kind": exc.metrics.limit_kind, "metrics": self._metrics_json(exc.metrics)},
                )
        else:
            reason = str(exc)
        self.events.emit(
            EventType.TOOL_FAILED,
            source=resource,
            target=pid,
            payload={"call_id": call_id, "error": reason, "policy_decision": "timeout"},
        )
        self.audit.record(
            actor=pid,
            action="tool.call",
            target=resource,
            decision={
                "ok": False,
                "tool": handle.name,
                "policy_decision": "timeout" if charge_error is None else "resource_limit",
                "error": reason,
                "metrics": self._metrics_json(exc.metrics),
            },
        )
        return reason

    def _metrics_json(self, metrics: CommandMetrics | None) -> dict[str, Any] | None:
        if metrics is None:
            return None
        return {
            "wall_seconds": metrics.wall_seconds,
            "cpu_seconds": metrics.cpu_seconds,
            "peak_memory_bytes": metrics.peak_memory_bytes,
            "killed": metrics.killed,
            "limit_kind": metrics.limit_kind,
        }

    def _metrics_from_validation(self, value: Any) -> CommandMetrics | None:
        if not isinstance(value, dict):
            return None
        return CommandMetrics(
            wall_seconds=float(value.get("wall_seconds") or 0.0),
            cpu_seconds=float(value.get("cpu_seconds") or 0.0),
            peak_memory_bytes=int(value.get("peak_memory_bytes") or 0),
            killed=bool(value.get("killed", False)),
            limit_kind=str(value["limit_kind"]) if value.get("limit_kind") is not None else None,
        )

    def _elapsed(self, started_at: float) -> float:
        return max(0.0, time.perf_counter() - started_at)

    def resolve(self, tool: ToolHandle | str, pid: str | None = None) -> ToolHandle:
        if isinstance(tool, ToolHandle):
            return tool
        process_tool_id: str | None = None
        if pid is not None:
            process = self.store.get_process(pid)
            if process is not None and tool in process.tool_table:
                process_tool_id = process.tool_table[tool]
                if process_tool_id in self._handles:
                    return self._handles[process_tool_id]
        if tool in self._handles:
            handle = self._handles[tool]
            if pid is None and handle.tool_id in self._jit_sources:
                raise NotFound(f"tool not found: {tool}")
            return handle
        if tool in self._tool_ids_by_name:
            return self._handles[self._tool_ids_by_name[tool]]
        for row in self.store.list_tools():
            row_tool_id = str(row["tool_id"])
            is_direct_id = row_tool_id == tool
            is_process_local_name = process_tool_id is not None and row_tool_id == process_tool_id
            is_name_match = row["name"] == tool
            if not (is_direct_id or is_name_match):
                continue
            if bool(row["ephemeral"]) and pid is None:
                continue
            if row_tool_id not in self._tools and row_tool_id not in self._jit_sources:
                raise NotFound(f"tool implementation not loaded: {row_tool_id}")
            handle = ToolHandle(tool_id=row_tool_id, name=row["name"], capability_id=None, scope=row["scope"])
            self._handles[handle.tool_id] = handle
            if not bool(row["ephemeral"]):
                self._tool_ids_by_name.setdefault(handle.name, handle.tool_id)
            return handle
        raise NotFound(f"tool not found: {tool}")

    def list(self) -> builtins.list[dict[str, Any]]:
        return self.store.list_tools()

    def visible_tools(self, pid: str) -> builtins.list[dict[str, Any]]:
        visible_ids = self._visible_tool_ids(pid)
        return [row for row in self.store.list_tools() if row["tool_id"] in visible_ids]

    def openai_tool_schemas(self, pid: str | None = None) -> builtins.list[dict[str, Any]]:
        tool_ids = self._visible_tool_ids(pid) if pid is not None else set(self._tools)
        schemas: builtins.list[dict[str, Any]] = []
        for tool_id in tool_ids:
            if tool_id in self._tools:
                schemas.append(self._tools[tool_id].to_openai_chat_tool())
                continue
            if tool_id not in self._jit_sources:
                continue
            spec = self.store.get_tool_spec(tool_id)
            if spec is None:
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.input_schema,
                    },
                }
            )
        return schemas

    def _context(self, pid: str, handle: ToolHandle, call_id: str) -> ToolContext:
        return ToolContext(
            trace_id=call_id,
            call_id=call_id,
            pid=pid,
            workspace_id=str(self.workspace_root),
            runtime=getattr(self, "runtime", None),
            metadata={
                "tool_id": handle.tool_id,
                "tool_name": handle.name,
            },
        )

    def _get_candidate(self, candidate_id: str) -> ToolCandidate:
        candidate = self.store.get_tool_candidate(candidate_id)
        if candidate is None:
            raise NotFound(f"tool candidate not found: {candidate_id}")
        return candidate

    def _require_candidate_owner(self, candidate: ToolCandidate, pid: str) -> None:
        if candidate.pid != pid:
            raise ValidationError(
                f"tool candidate {candidate.candidate_id} belongs to process {candidate.pid}, not {pid}"
            )

    def _name_collides_with_static_tool(self, name: str) -> bool:
        mapped = self._tool_ids_by_name.get(name)
        if mapped in self._tools:
            return True
        return any(row["name"] == name and not bool(row["ephemeral"]) for row in self.store.list_tools())

    def _process_has_tool(self, pid: str, handle: ToolHandle) -> bool:
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        return process.tool_table.get(handle.name) == handle.tool_id

    def _terminal_process_error(self, pid: str) -> str | None:
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        if process.status in _TERMINAL_PROCESS_STATUSES:
            return f"cannot call tools for terminal process {pid}: {process.status.value}"
        return None

    def _visible_tool_ids(self, pid: str) -> set[str]:
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        return set(process.tool_table.values())


def _stable_static_tool_id(name: str, digest_chars: int = _TOOL_DEFAULTS.static_tool_id_digest_chars) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:digest_chars]
    return f"tool_static_{digest}"

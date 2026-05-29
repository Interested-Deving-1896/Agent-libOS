from __future__ import annotations

import asyncio
import builtins
from pathlib import Path
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.exceptions import HumanApprovalRequired, NotFound, ValidationError
from agent_libos.human.manager import HumanObjectManager
from agent_libos.ids import new_id, utc_now
from agent_libos.memory.object_memory import ObjectMemoryManager
from agent_libos.models import (
    EventType,
    ObjectMetadata,
    ObjectType,
    ToolCallResult,
    ToolCandidate,
    ToolCandidateStatus,
    ToolHandle,
    ToolSpec,
    ValidationResult,
)
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.storage import SQLiteStore
from agent_libos.tools.base import BaseAgentTool, ToolContext
from agent_libos.tools.sandbox import PythonSubprocessSandbox, SandboxBackend


class ToolBroker:
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
    ):
        self.store = store
        self.memory = memory
        self.capabilities = capabilities
        self.human = human
        self.audit = audit
        self.events = events
        self.sandbox = sandbox or PythonSubprocessSandbox()
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
        tool_id = new_id("tool")
        spec = tool.spec()
        if spec.name in self._tool_ids_by_name:
            raise ValueError(f"tool already registered: {spec.name}")
        handle = ToolHandle(tool_id=tool_id, name=spec.name, capability_id=None, scope=scope)
        self._tools[tool_id] = tool
        self._tool_ids_by_name[spec.name] = tool_id
        self._handles[tool_id] = handle
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
                "tool execute capabilities are not used in the current MVP; configure process tools at creation time"
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

    def validate(self, candidate_id: str) -> ValidationResult:
        candidate = self._get_candidate(candidate_id)
        result = self.sandbox.run_tests(candidate.source_code, candidate.tests)
        errors = list(result.errors)
        warnings = list(result.warnings)
        if candidate.requested_capabilities:
            errors.append("MVP JIT tools cannot request external capabilities")
        validation = ValidationResult(ok=not errors and result.ok, errors=errors, warnings=warnings, logs=result.logs)
        candidate.validation = {
            "ok": validation.ok,
            "errors": validation.errors,
            "warnings": validation.warnings,
            "logs": validation.logs,
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
        if candidate.status != ToolCandidateStatus.VALIDATED:
            validation = self.validate(candidate_id)
            if not validation.ok:
                raise ValidationError("; ".join(validation.errors))
            candidate = self._get_candidate(candidate_id)
        tool_id = new_id("tool")
        handle = ToolHandle(tool_id=tool_id, name=candidate.spec.name, capability_id=None, scope=scope)
        self._jit_sources[tool_id] = candidate.source_code
        self._handles[tool_id] = handle
        self._tool_ids_by_name[candidate.spec.name] = tool_id
        self.store.insert_tool(handle, candidate.spec, registered_by=approver, created_at=utc_now(), ephemeral=True)
        candidate.status = ToolCandidateStatus.REGISTERED
        candidate.updated_at = utc_now()
        self.store.update_tool_candidate(candidate)
        process = self.store.get_process(pid)
        if process is not None:
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
        if not self._process_has_tool(pid, handle):
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
        self.events.emit(
            EventType.TOOL_CALLED,
            source=pid,
            target=resource,
            payload={"call_id": call_id, "args": args},
        )
        try:
            if handle.tool_id in self._tools:
                tool_result = await self._tools[handle.tool_id].ainvoke(args, self._context(pid, handle, call_id))
                if not tool_result.ok:
                    error_message = tool_result.error.message if tool_result.error else tool_result.content
                    self.events.emit(
                        EventType.TOOL_FAILED,
                        source=resource,
                        target=pid,
                        payload={"call_id": call_id, "error": error_message, "tool_result": tool_result.model_dump(mode="json")},
                    )
                    self.audit.record(
                        actor=pid,
                        action="tool.call",
                        target=resource,
                        decision={
                            "ok": False,
                            "tool": handle.name,
                            "policy_decision": "allow",
                            "tool_result": tool_result.model_dump(mode="json"),
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
                payload = await asyncio.to_thread(self.sandbox.run_source, self._jit_sources[handle.tool_id], args)
                result_payload = {"tool_id": handle.tool_id, "tool_name": handle.name, "result": payload}
            else:
                raise NotFound(f"tool implementation not loaded: {handle.tool_id}")
        except HumanApprovalRequired as exc:
            self.audit.record(
                actor=pid,
                action="tool.call_waiting_human",
                target=resource,
                decision={
                    "ok": False,
                    "tool": handle.name,
                    "policy_decision": "require_human_approval",
                    "request_id": exc.request_id,
                },
            )
            raise
        except Exception as exc:
            self.events.emit(
                EventType.TOOL_FAILED,
                source=resource,
                target=pid,
                payload={"call_id": call_id, "error": str(exc)},
            )
            self.audit.record(
                actor=pid,
                action="tool.call",
                target=resource,
                decision={"ok": False, "tool": handle.name, "policy_decision": "allow", "error": str(exc)},
            )
            return ToolCallResult(call_id=call_id, tool_id=handle.tool_id, result_handle=None, payload=None, ok=False, error=str(exc))

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
            decision={"ok": True, "tool": handle.name, "policy_decision": "allow"},
        )
        return ToolCallResult(
            call_id=call_id,
            tool_id=handle.tool_id,
            result_handle=result_handle,
            payload=payload,
            ok=True,
        )

    def resolve(self, tool: ToolHandle | str, pid: str | None = None) -> ToolHandle:
        if isinstance(tool, ToolHandle):
            return tool
        if pid is not None:
            process = self.store.get_process(pid)
            if process is not None and tool in process.tool_table:
                tool_id = process.tool_table[tool]
                if tool_id in self._handles:
                    return self._handles[tool_id]
        if tool in self._handles:
            return self._handles[tool]
        if tool in self._tool_ids_by_name:
            return self._handles[self._tool_ids_by_name[tool]]
        for row in self.store.list_tools():
            if row["tool_id"] == tool or row["name"] == tool:
                if row["tool_id"] not in self._tools and row["tool_id"] not in self._jit_sources:
                    raise NotFound(f"tool implementation not loaded: {row['tool_id']}")
                handle = ToolHandle(tool_id=row["tool_id"], name=row["name"], capability_id=None, scope=row["scope"])
                self._handles[handle.tool_id] = handle
                self._tool_ids_by_name.setdefault(handle.name, handle.tool_id)
                return handle
        raise NotFound(f"tool not found: {tool}")

    def list(self) -> builtins.list[dict[str, Any]]:
        return self.store.list_tools()

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
        tool = self._tools[handle.tool_id]
        return ToolContext(
            trace_id=call_id,
            call_id=call_id,
            pid=pid,
            workspace_id=str(self.workspace_root),
            runtime=getattr(self, "runtime", None),
            granted_permissions=set(tool.policy.permissions),
            metadata={
                "tool_id": handle.tool_id,
                "tool_name": handle.name,
                "confirmed": True,
            },
        )

    def _get_candidate(self, candidate_id: str) -> ToolCandidate:
        candidate = self.store.get_tool_candidate(candidate_id)
        if candidate is None:
            raise NotFound(f"tool candidate not found: {candidate_id}")
        return candidate

    def _process_has_tool(self, pid: str, handle: ToolHandle) -> bool:
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        return process.tool_table.get(handle.name) == handle.tool_id

    def _visible_tool_ids(self, pid: str) -> set[str]:
        process = self.store.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        return set(process.tool_table.values())

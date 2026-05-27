from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.exceptions import HumanApprovalRequired, NotFound, SandboxError, ValidationError
from agent_libos.human.manager import HumanObjectManager
from agent_libos.ids import new_id, utc_now
from agent_libos.memory.object_memory import ObjectMemoryManager
from agent_libos.models import (
    CapabilityRight,
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
from agent_libos.tools.sandbox import PythonSubprocessSandbox, SandboxBackend


StaticHandler = Callable[[dict[str, Any]], Any]


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
    ):
        self.store = store
        self.memory = memory
        self.capabilities = capabilities
        self.human = human
        self.audit = audit
        self.events = events
        self.sandbox = sandbox or PythonSubprocessSandbox()
        self._handlers: dict[str, StaticHandler] = {}
        self._handles: dict[str, ToolHandle] = {}
        self._jit_sources: dict[str, str] = {}

    def register_static(
        self,
        name: str,
        handler: StaticHandler,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        side_effects: list[str] | None = None,
        registered_by: str = "runtime",
    ) -> ToolHandle:
        tool_id = new_id("tool")
        spec = ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema or {},
            output_schema=output_schema or {},
            side_effects=side_effects or [],
        )
        handle = ToolHandle(tool_id=tool_id, name=name, capability_id=None, scope="static")
        self._handlers[tool_id] = handler
        self._handles[tool_id] = handle
        self.store.insert_tool(handle, spec, registered_by=registered_by, created_at=utc_now(), ephemeral=False)
        self.audit.record(
            actor=registered_by,
            action="tool.register_static",
            target=f"tool:{tool_id}",
            decision={"name": name, "side_effects": side_effects or []},
        )
        return handle

    def grant_execute(self, pid: str, tool: ToolHandle | str, issued_by: str = "tool_broker") -> str:
        handle = self.resolve(tool)
        cap = self.capabilities.grant(
            subject=pid,
            resource=f"tool:{handle.tool_id}",
            rights=[CapabilityRight.EXECUTE],
            issued_by=issued_by,
        )
        return cap.cap_id

    def propose(
        self,
        pid: str,
        spec: ToolSpec | dict[str, Any],
        source_code: str,
        tests: list[dict[str, Any]] | None = None,
        requested_capabilities: list[dict[str, Any]] | None = None,
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
            pid,
            ObjectType.TOOL_CANDIDATE,
            {
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
        cap = self.capabilities.grant(
            subject=pid,
            resource=f"tool:{tool_id}",
            rights=[CapabilityRight.EXECUTE],
            issued_by=approver,
        )
        handle = ToolHandle(tool_id=tool_id, name=candidate.spec.name, capability_id=cap.cap_id, scope=scope)
        self._jit_sources[tool_id] = candidate.source_code
        self._handles[tool_id] = handle
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
            capability_refs=[cap.cap_id],
            decision={"candidate_id": candidate_id, "scope": scope},
        )
        return handle

    def call(self, pid: str, tool: ToolHandle | str, args: dict[str, Any]) -> ToolCallResult:
        handle = self.resolve(tool)
        resource = f"tool:{handle.tool_id}"
        if not self.capabilities.check(pid, resource, CapabilityRight.EXECUTE):
            request_id = self.human.query(
                pid=pid,
                human="owner",
                request={
                    "type": "approval",
                    "question": f"Grant execute capability for tool {handle.name}?",
                    "requested_capability": {
                        "subject": pid,
                        "resource": resource,
                        "rights": [CapabilityRight.EXECUTE.value],
                    },
                    "context": {"tool_id": handle.tool_id, "tool_name": handle.name},
                },
                blocking=True,
            )
            raise HumanApprovalRequired(request_id, f"tool execution requires approval: {handle.name}")

        call_id = new_id("tcall")
        self.events.emit(
            EventType.TOOL_CALLED,
            source=pid,
            target=resource,
            payload={"call_id": call_id, "args": args},
        )
        try:
            if handle.tool_id in self._handlers:
                payload = self._handlers[handle.tool_id](args)
            elif handle.tool_id in self._jit_sources:
                payload = self.sandbox.run_source(self._jit_sources[handle.tool_id], args)
            else:
                raise NotFound(f"tool implementation not loaded: {handle.tool_id}")
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
                decision={"ok": False, "error": str(exc)},
            )
            return ToolCallResult(call_id=call_id, tool_id=handle.tool_id, result_handle=None, payload=None, ok=False, error=str(exc))

        result_handle = self.memory.create_object(
            pid=pid,
            type=ObjectType.TOOL_RESULT,
            payload={"tool_id": handle.tool_id, "tool_name": handle.name, "result": payload},
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
            decision={"ok": True, "tool": handle.name},
        )
        return ToolCallResult(
            call_id=call_id,
            tool_id=handle.tool_id,
            result_handle=result_handle,
            payload=payload,
            ok=True,
        )

    def resolve(self, tool: ToolHandle | str) -> ToolHandle:
        if isinstance(tool, ToolHandle):
            return tool
        if tool in self._handles:
            return self._handles[tool]
        for handle in self._handles.values():
            if handle.name == tool:
                return handle
        for row in self.store.list_tools():
            if row["tool_id"] == tool or row["name"] == tool:
                handle = ToolHandle(tool_id=row["tool_id"], name=row["name"], capability_id=None, scope=row["scope"])
                self._handles[handle.tool_id] = handle
                return handle
        raise NotFound(f"tool not found: {tool}")

    def list(self) -> list[dict[str, Any]]:
        return self.store.list_tools()

    def _get_candidate(self, candidate_id: str) -> ToolCandidate:
        candidate = self.store.get_tool_candidate(candidate_id)
        if candidate is None:
            raise NotFound(f"tool candidate not found: {candidate_id}")
        return candidate


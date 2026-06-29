from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import math
import os
import re
import socket
import time
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema.exceptions import SchemaError as JsonSchemaSchemaError
from jsonschema.validators import validator_for as jsonschema_validator_for

from agent_libos.capability.manager import CapabilityManager
from agent_libos.capability.rules import AUTHORITY_RULES_KEY
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.human.manager import HumanObjectManager
from agent_libos.models import (
    AuditRecord,
    CapabilityEffect,
    CapabilityRight,
    Event,
    EventType,
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    McpCallResult,
    McpCallStatus,
    McpHeaderSpec,
    McpHttpTransportSpec,
    McpProviderCallResult,
    McpProviderTool,
    McpServerSpec,
    McpStdioTransportSpec,
    McpToolSpec,
    ResourceUsage,
)
from agent_libos.models.exceptions import CapabilityDenied, HumanApprovalRequired, NotFound, ValidationError
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.runtime.external_effects import (
    classify_external_effect,
    record_external_effect,
    require_external_effect_classifier,
)
from agent_libos.storage import RuntimeStore
from agent_libos.substrate import McpProvider
from agent_libos.tools.observability import sanitize_for_observability
from agent_libos.utils.ids import utc_now
from agent_libos.utils.serde import dumps, to_jsonable
from agent_libos.utils.yaml_loader import load_yaml_mapping

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]*$")
_ENV_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_FORBIDDEN_HEADERS = {"connection", "content-length", "host", "transfer-encoding", "upgrade"}
_FORBIDDEN_MCP_HOSTS = {"metadata.google.internal"}
_LOCAL_HTTP_HOSTS = {"localhost", "127.0.0.1", "::1"}
_CALL_RIGHTS = {CapabilityRight.READ.value, CapabilityRight.WRITE.value, CapabilityRight.EXECUTE.value}
_ALLOWED_HEADER_PREFIXES = {"", "Bearer ", "Token ", "Basic "}
_ALLOWED_HEADER_SUFFIXES = {""}
_TRANSPORTS = {"stdio", "streamable_http"}


class McpPrimitive:
    """Capability-controlled MCP client primitive for registered external servers."""

    def __init__(
        self,
        store: RuntimeStore,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        *,
        human: HumanObjectManager | None,
        provider: McpProvider,
        config: AgentLibOSConfig | None = None,
        resources: Any | None = None,
    ) -> None:
        self.config = config or DEFAULT_CONFIG
        self.store = store
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        self.human = human
        self.provider = provider
        self.resources = resources

    def server_resource(self, server_id: str) -> str:
        return f"mcp_server:{server_id}"

    def tool_resource(self, server_id: str, tool_id: str) -> str:
        return f"mcp:{server_id}:{tool_id}"

    def register_server(
        self,
        server: McpServerSpec | dict[str, Any],
        *,
        actor: str = "runtime",
        replace: bool = False,
        require_capability: bool = True,
        source: str | None = None,
    ) -> dict[str, Any]:
        spec = self._coerce_server(server)
        existing = self.store.get_mcp_server(spec.server_id)
        if existing is not None and not replace:
            raise ValidationError(f"MCP server already exists: {spec.server_id}")
        if require_capability:
            required_right = CapabilityRight.ADMIN if existing is not None else CapabilityRight.WRITE
            self.capabilities.require(actor, self.server_resource(spec.server_id), required_right)
        now = utc_now()
        self.store.upsert_mcp_server(spec, registered_by=actor, created_at=now)
        if existing is not None:
            self._disable_replaced_server_tool_capabilities(spec.server_id, actor=actor)
        self.events.emit(
            EventType.EXTERNAL_WRITE,
            source=actor,
            target=self.server_resource(spec.server_id),
            payload={"adapter": "mcp", "operation": "server_register", "server_id": spec.server_id},
        )
        self.audit.record(
            actor=actor,
            action="mcp.server.register" if existing is None else "mcp.server.replace",
            target=self.server_resource(spec.server_id),
            decision={
                "server_id": spec.server_id,
                "transport": spec.transport,
                "tools": [tool.tool_id for tool in spec.tools],
                "replaced": existing is not None,
                "source": source,
            },
        )
        return self.inspect_server(spec.server_id, actor=actor, require_capability=False)

    def register_server_from_yaml_text(
        self,
        text: str,
        *,
        actor: str,
        replace: bool = False,
        require_capability: bool = True,
        source: str | None = None,
    ) -> dict[str, Any]:
        if len(text.encode("utf-8")) > self.config.mcp.manifest_max_bytes:
            raise ValidationError(f"MCP manifest exceeds manifest_max_bytes={self.config.mcp.manifest_max_bytes}")
        data = load_yaml_mapping(text)
        if set(data) == {"mcp_server"} and isinstance(data["mcp_server"], dict):
            data = data["mcp_server"]
        if set(data) == {"server"} and isinstance(data["server"], dict):
            data = data["server"]
        return self.register_server(
            data,
            actor=actor,
            replace=replace,
            require_capability=require_capability,
            source=source,
        )

    def list_servers(
        self,
        *,
        actor: str | None = None,
        require_capability: bool = True,
        text: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if require_capability and actor is not None:
            self.capabilities.require(actor, self.config.mcp.registry_resource, CapabilityRight.READ)
        selected_limit = self._bounded_list_limit(limit)
        servers: list[dict[str, Any]] = []
        for spec, metadata in self.store.list_mcp_servers(text=text, limit=selected_limit):
            self._validate_server(spec)
            servers.append(self._server_to_json(spec, metadata, include_sensitive_fields=False))
        return servers

    def inspect_server(
        self,
        server_id: str,
        *,
        actor: str | None = None,
        require_capability: bool = True,
        include_sensitive_fields: bool = False,
    ) -> dict[str, Any]:
        spec, metadata = self._load_server(server_id)
        if require_capability and actor is not None:
            self.capabilities.require(actor, self.server_resource(server_id), CapabilityRight.READ)
        return self._server_to_json(spec, metadata, include_sensitive_fields=include_sensitive_fields)

    def list_tools(
        self,
        server_id: str,
        *,
        actor: str | None = None,
        require_capability: bool = True,
        refresh: bool = False,
    ) -> dict[str, Any]:
        spec, _metadata = self._load_server(server_id)
        if require_capability and actor is not None:
            self.capabilities.require(actor, self.server_resource(server_id), CapabilityRight.READ)
        live_by_name: dict[str, McpProviderTool] = {}
        live_response_bytes = 0
        if refresh:
            self._require_runtime_environment(spec)
            if spec.transport == "streamable_http":
                self._validate_runtime_resolution(spec)
            result = self.provider.list_tools(
                spec,
                timeout_s=spec.timeout_s,
                max_response_bytes=spec.max_response_bytes,
            )
            live_response_bytes = result.response_bytes
            live_by_name = {tool.name: tool for tool in result.tools}
        return {
            "server_id": spec.server_id,
            "transport": spec.transport,
            "tools": [
                self._tool_to_json(spec.server_id, tool, live=live_by_name.get(tool.mcp_name) if refresh else None)
                for tool in spec.tools
            ],
            "refreshed": refresh,
            "response_bytes": live_response_bytes,
        }

    def unregister_server(
        self,
        server_id: str,
        *,
        actor: str = "runtime",
        require_capability: bool = True,
    ) -> dict[str, Any]:
        self._load_server(server_id)
        if require_capability:
            self.capabilities.require(actor, self.server_resource(server_id), CapabilityRight.ADMIN)
        self._disable_replaced_server_tool_capabilities(server_id, actor=actor)
        self.store.delete_mcp_server(server_id)
        self.audit.record(
            actor=actor,
            action="mcp.server.unregister",
            target=self.server_resource(server_id),
            decision={"server_id": server_id},
        )
        return {"server_id": server_id, "deleted": True}

    def call_tool(self, pid: str, server_id: str, tool_id: str, arguments: Any = None) -> McpCallResult:
        spec, _metadata = self._load_server(server_id)
        tool = spec.tool_by_id(tool_id)
        if tool is None:
            raise NotFound(f"MCP tool not found: {server_id}/{tool_id}")
        selected_args = {} if arguments is None else arguments
        if not isinstance(selected_args, dict):
            raise ValidationError("MCP tool arguments must be a JSON object or null")
        self._validate_json_value(selected_args, "arguments")
        self._validate_arguments_against_schema(tool, selected_args)
        resource = self.tool_resource(server_id, tool_id)
        operation_context = self._operation_context(pid, spec, tool, selected_args)
        decision = self._authorize_call(pid, resource, tool.right, operation_context)
        profile = self.capabilities.profiles.mcp(
            resource=resource,
            effect=decision.effect or CapabilityEffect.DENY,
            server_id=server_id,
            tool_id=tool_id,
        )
        operation_context.update(
            {
                "capability_ids": list(decision.matched_capability_ids),
                "selected_capability_id": decision.selected_capability_id,
                "sandbox_profile": self._profile_json(profile),
            }
        )
        self._require_runtime_environment(spec)
        if spec.transport == "streamable_http":
            self._validate_runtime_resolution(spec)
        request_bytes = len(dumps({"name": tool.mcp_name, "arguments": selected_args}).encode("utf-8"))
        if request_bytes > spec.max_request_bytes:
            raise ValidationError(f"MCP request exceeds max_request_bytes={spec.max_request_bytes}")
        self._preflight_resource_usage(
            pid,
            ResourceUsage(mcp_request_bytes=request_bytes),
            source="primitive.mcp.call",
            context={"server_id": server_id, "tool_id": tool_id, "request_bytes": request_bytes},
        )
        self._validate_live_tool(spec, tool)
        effect_context = self._effect_context(spec, tool, operation_context, request_bytes=request_bytes)
        require_external_effect_classifier(self.provider, "call_tool")
        preflight_classification = classify_external_effect(self.provider, "call_tool", effect_context, {"preflight": True})
        self.capabilities.claim_decision_use(
            decision,
            used_by="mcp",
            reason="one-time MCP tool permission consumed",
        )
        started = time.monotonic()
        try:
            provider_result = self.provider.call_tool(
                spec,
                tool,
                selected_args,
                timeout_s=spec.timeout_s,
                max_response_bytes=spec.max_response_bytes,
            )
        except Exception as exc:
            provider_result = McpProviderCallResult(
                error=f"{type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - started,
            )
        result = self._call_result_from_provider(spec, tool, provider_result)
        event = self._emit_call_event(pid, resource, result, tool)
        audit_record = self._record_call_audit(pid, resource, result, tool, operation_context)
        self._record_external_effect(
            pid,
            resource,
            effect_context,
            result,
            event,
            audit_record,
            preflight_classification=preflight_classification,
        )
        self._charge_resource_usage(
            pid,
            ResourceUsage(mcp_request_bytes=request_bytes, mcp_response_bytes=result.response_bytes),
            source="primitive.mcp.call",
            context={
                "server_id": server_id,
                "tool_id": tool_id,
                "request_bytes": request_bytes,
                "response_bytes": result.response_bytes,
                "status": result.status.value,
            },
        )
        return result

    async def acall_tool(self, pid: str, server_id: str, tool_id: str, arguments: Any = None) -> McpCallResult:
        return await asyncio.to_thread(self.call_tool, pid, server_id, tool_id, arguments)

    def grant_tool(
        self,
        pid: str,
        server_id: str,
        tool_id: str,
        *,
        right: str | CapabilityRight,
        issued_by: str = "mcp",
        delegable: bool = True,
    ) -> Any:
        return self.capabilities.grant(
            subject=pid,
            resource=self.tool_resource(server_id, tool_id),
            rights=[CapabilityRight(str(right))],
            issued_by=issued_by,
            delegable=delegable,
        )

    def _authorize_call(self, pid: str, resource: str, right: str, context: dict[str, Any]) -> Any:
        decision = self.capabilities.authorize(pid, resource, right, context, audit=True)
        if decision.allowed:
            return decision
        if decision.policy == CapabilityManager.ASK_EACH_TIME:
            if self.human is None:
                raise CapabilityDenied(f"{pid} requires human approval for MCP call on {resource}")
            profile = self.capabilities.profiles.mcp(
                resource=resource,
                effect=CapabilityEffect.ASK,
                server_id=str(context["server_id"]),
                tool_id=str(context["tool_id"]),
            )
            approval_context = {**context, "sandbox_profile": self._profile_json(profile)}
            request_id = self.human.query(
                pid=pid,
                human=self.config.runtime.default_human,
                request={
                    "type": "external_operation_approval",
                    "question": f"Allow this process to call MCP tool {resource}?",
                    "requested_once_capability": {
                        "subject": pid,
                        "resource": resource,
                        "rights": [right],
                        "constraints": self._approval_constraints(context),
                    },
                    "context": approval_context,
                },
                blocking=True,
            )
            raise HumanApprovalRequired(
                request_id=request_id,
                message=f"{pid} is waiting for per-use human approval to call {resource}",
            )
        raise CapabilityDenied(decision.reason)

    def _validate_live_tool(self, server: McpServerSpec, tool: McpToolSpec) -> None:
        result = self.provider.list_tools(
            server,
            timeout_s=server.timeout_s,
            max_response_bytes=server.max_response_bytes,
        )
        live = next((item for item in result.tools if item.name == tool.mcp_name), None)
        if live is None:
            raise ValidationError(f"MCP server {server.server_id} no longer exposes tool {tool.mcp_name}")
        if tool.input_schema and live.input_schema and live.input_schema != tool.input_schema:
            raise ValidationError(f"MCP tool schema changed for {server.server_id}/{tool.tool_id}")

    def _call_result_from_provider(
        self,
        server: McpServerSpec,
        tool: McpToolSpec,
        provider_result: McpProviderCallResult,
    ) -> McpCallResult:
        if provider_result.error:
            return self._failure(server, tool, McpCallStatus.TRANSPORT_ERROR, provider_result.error, provider_result)
        if provider_result.too_large:
            return self._failure(
                server,
                tool,
                McpCallStatus.RESPONSE_TOO_LARGE,
                f"response exceeded max_response_bytes={server.max_response_bytes}",
                provider_result,
            )
        if provider_result.is_error:
            return self._failure(
                server,
                tool,
                McpCallStatus.MCP_ERROR,
                "MCP tool returned an error result",
                provider_result,
                extra={"content": provider_result.content},
            )
        return McpCallResult(
            server_id=server.server_id,
            tool_id=tool.tool_id,
            mcp_name=tool.mcp_name,
            status=McpCallStatus.OK,
            ok=True,
            result={
                "content": to_jsonable(provider_result.content),
                "structured_content": to_jsonable(provider_result.structured_content),
            },
            response_bytes=provider_result.response_bytes,
            duration_s=provider_result.duration_s,
        )

    def _failure(
        self,
        server: McpServerSpec,
        tool: McpToolSpec,
        status: McpCallStatus,
        message: str,
        provider_result: McpProviderCallResult,
        *,
        extra: dict[str, Any] | None = None,
    ) -> McpCallResult:
        return McpCallResult(
            server_id=server.server_id,
            tool_id=tool.tool_id,
            mcp_name=tool.mcp_name,
            status=status,
            ok=False,
            error={"message": message, **dict(extra or {})},
            response_bytes=provider_result.response_bytes,
            duration_s=provider_result.duration_s,
        )

    def _emit_call_event(
        self,
        pid: str,
        resource: str,
        result: McpCallResult,
        tool: McpToolSpec,
    ) -> Event:
        event_type = EventType.EXTERNAL_WRITE if tool.state_mutation or tool.right != CapabilityRight.READ.value else EventType.EXTERNAL_READ
        return self.events.emit(
            event_type,
            source=pid,
            target=resource,
            payload={
                "adapter": "mcp",
                "server_id": result.server_id,
                "tool_id": result.tool_id,
                "mcp_name": result.mcp_name,
                "status": result.status.value,
                "ok": result.ok,
                "response_bytes": result.response_bytes,
                "duration_s": result.duration_s,
            },
        )

    def _record_call_audit(
        self,
        pid: str,
        resource: str,
        result: McpCallResult,
        tool: McpToolSpec,
        operation_context: dict[str, Any],
    ) -> AuditRecord:
        return self.audit.record(
            actor=pid,
            action="primitive.mcp.call",
            target=resource,
            decision={
                "server_id": result.server_id,
                "tool_id": result.tool_id,
                "mcp_name": tool.mcp_name,
                "right": tool.right,
                "arguments_sha256": operation_context["arguments_sha256"],
                "arguments_preview": operation_context["arguments_preview"],
                "arguments_observation": operation_context["arguments_observation"],
                "sandbox_profile": operation_context.get("sandbox_profile"),
                "status": result.status.value,
                "ok": result.ok,
                "response_bytes": result.response_bytes,
                "duration_s": result.duration_s,
            },
            capability_refs=list(operation_context.get("capability_ids") or []),
        )

    def _record_external_effect(
        self,
        pid: str,
        resource: str,
        context: dict[str, Any],
        result: McpCallResult,
        event: Event,
        audit_record: AuditRecord,
        *,
        preflight_classification: ExternalEffectClassification,
    ) -> None:
        result_payload = {
            "status": result.status.value,
            "ok": result.ok,
            "response_bytes": result.response_bytes,
            "duration_s": result.duration_s,
        }
        try:
            classification = classify_external_effect(self.provider, "call_tool", context, result_payload)
        except Exception as exc:
            classification = ExternalEffectClassification(
                rollback_class=ExternalEffectRollbackClass.IRREVERSIBLE,
                rollback_status=ExternalEffectRollbackStatus.NOT_SUPPORTED,
                state_mutation=True,
                information_flow=True,
                metadata={
                    **dict(preflight_classification.metadata),
                    "classification_error": f"{type(exc).__name__}: {exc}",
                    "classification_fallback": "post_call_failure",
                },
            )
        record_external_effect(
            self.store,
            pid=pid,
            provider="mcp",
            operation="call_tool",
            target=resource,
            classification=classification,
            audit_record=audit_record,
            event=event,
            metadata={"context": context, "result": result_payload},
        )

    def _preflight_resource_usage(
        self,
        pid: str,
        usage: ResourceUsage,
        *,
        source: str,
        context: dict[str, Any],
    ) -> None:
        if self.resources is None:
            return
        self.resources.preflight(pid, usage, source=source, context=context)

    def _charge_resource_usage(
        self,
        pid: str,
        usage: ResourceUsage,
        *,
        source: str,
        context: dict[str, Any],
    ) -> None:
        if self.resources is None:
            return
        self.resources.charge(
            pid,
            usage,
            source=source,
            context=context,
            allow_overage=True,
            kill_on_exceed=True,
        )

    def _operation_context(
        self,
        pid: str,
        server: McpServerSpec,
        tool: McpToolSpec,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        arguments_json = dumps(arguments)
        arguments_observation = sanitize_for_observability(
            {"arguments": arguments},
            preview_chars=self.config.mcp.audit_preview_chars,
        )
        return {
            "pid": pid,
            "primitive": "runtime.mcp.call",
            "operation": "mcp.call",
            "authority_operation": "mcp.call",
            "server_id": server.server_id,
            "transport": server.transport,
            "tool_id": tool.tool_id,
            "mcp_name": tool.mcp_name,
            "right": tool.right,
            "arguments_sha256": hashlib.sha256(arguments_json.encode("utf-8")).hexdigest(),
            "arguments_preview": arguments_observation["preview"],
            "arguments_observation": arguments_observation,
        }

    def _approval_constraints(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            AUTHORITY_RULES_KEY: [
                {
                    "rule_id": f"mcp.approval.{context['server_id']}.{context['tool_id']}",
                    "operation": "mcp.call",
                    "effect": CapabilityEffect.ALLOW.value,
                    "risk": "high",
                    "conditions": {
                        "server_id": context["server_id"],
                        "tool_id": context["tool_id"],
                        "arguments_sha256": context["arguments_sha256"],
                    },
                    "description": "one-shot human approval for exact MCP tool payload",
                }
            ]
        }

    def _effect_context(
        self,
        server: McpServerSpec,
        tool: McpToolSpec,
        operation_context: dict[str, Any],
        *,
        request_bytes: int,
    ) -> dict[str, Any]:
        return {
            "server_id": server.server_id,
            "transport": server.transport,
            "tool_id": tool.tool_id,
            "mcp_name": tool.mcp_name,
            "right": tool.right,
            "rollback_class": tool.rollback_class,
            "rollback_status": tool.rollback_status,
            "state_mutation": tool.state_mutation,
            "information_flow": tool.information_flow,
            "arguments_sha256": operation_context["arguments_sha256"],
            "arguments_observation": operation_context["arguments_observation"],
            "request_bytes": request_bytes,
        }

    def _coerce_server(self, value: McpServerSpec | dict[str, Any]) -> McpServerSpec:
        if isinstance(value, McpServerSpec):
            spec = value
        elif isinstance(value, dict):
            transport = str(value.get("transport", "") or "").strip()
            server_id = self._required(value, "server_id", "MCP server")
            spec = McpServerSpec(
                schema_version=int(value.get("schema_version", 1)),
                server_id=str(server_id),
                transport=transport,
                stdio=self._stdio_spec(value.get("stdio")) if transport == "stdio" else None,
                http=self._http_spec(value.get("http")) if transport == "streamable_http" else None,
                tools=[self._tool_spec(item) for item in list(value.get("tools") or [])],
                timeout_s=self._coerce_positive_float(value.get("timeout_s", self.config.mcp.timeout_s), "timeout_s"),
                max_request_bytes=self._coerce_positive_int(
                    value.get("max_request_bytes", self.config.mcp.max_request_bytes),
                    "max_request_bytes",
                ),
                max_response_bytes=self._coerce_positive_int(
                    value.get("max_response_bytes", self.config.mcp.max_response_bytes),
                    "max_response_bytes",
                ),
                metadata=dict(value.get("metadata") or {}),
            )
        else:
            raise ValidationError("MCP server must be an object")
        self._validate_server(spec)
        return spec

    def _stdio_spec(self, value: Any) -> McpStdioTransportSpec:
        if not isinstance(value, dict):
            raise ValidationError("MCP stdio transport requires stdio object")
        return McpStdioTransportSpec(
            command=str(value.get("command", "")),
            args=[str(item) for item in list(value.get("args") or [])],
            env={str(name): str(host_name) for name, host_name in dict(value.get("env") or {}).items()},
            cwd=str(value["cwd"]) if value.get("cwd") is not None else None,
        )

    def _http_spec(self, value: Any) -> McpHttpTransportSpec:
        if not isinstance(value, dict):
            raise ValidationError("MCP streamable_http transport requires http object")
        return McpHttpTransportSpec(
            url=str(value.get("url", "")),
            headers=self._header_specs(value.get("headers") or {}),
        )

    def _tool_spec(self, value: Any) -> McpToolSpec:
        if not isinstance(value, dict):
            raise ValidationError("MCP tools entries must be objects")
        return McpToolSpec(
            tool_id=str(self._required(value, "tool_id", "MCP tool")),
            mcp_name=str(self._required(value, "mcp_name", "MCP tool")),
            right=str(self._required(value, "right", "MCP tool")),
            rollback_class=str(self._required(value, "rollback_class", "MCP tool")),
            rollback_status=value.get("rollback_status"),
            state_mutation=self._coerce_bool(
                self._required(value, "state_mutation", "MCP tool"),
                "state_mutation",
            ),
            information_flow=self._coerce_bool(
                self._required(value, "information_flow", "MCP tool"),
                "information_flow",
            ),
            input_schema=dict(value.get("input_schema") or {}),
            metadata=dict(value.get("metadata") or {}),
        )

    def _validate_server(self, server: McpServerSpec) -> None:
        if server.schema_version != 1:
            raise ValidationError("MCP server schema_version must be 1")
        self._validate_identifier(server.server_id, "server_id", self.config.mcp.server_id_max_chars)
        if server.transport not in _TRANSPORTS:
            raise ValidationError("MCP transport must be stdio or streamable_http")
        if server.transport == "stdio":
            self._validate_stdio(server.stdio)
            if server.http is not None:
                raise ValidationError("MCP stdio server cannot include http configuration")
        if server.transport == "streamable_http":
            self._validate_http(server.http)
            if server.stdio is not None:
                raise ValidationError("MCP streamable_http server cannot include stdio configuration")
        if not server.tools:
            raise ValidationError("MCP server must declare at least one allowed tool")
        if server.timeout_s > self.config.mcp.timeout_hard_limit_s:
            raise ValidationError("MCP timeout_s exceeds configured hard limit")
        if server.max_request_bytes > self.config.mcp.max_request_hard_limit_bytes:
            raise ValidationError("MCP max_request_bytes exceeds configured hard limit")
        if server.max_response_bytes > self.config.mcp.max_response_hard_limit_bytes:
            raise ValidationError("MCP max_response_bytes exceeds configured hard limit")
        seen_tool_ids: set[str] = set()
        seen_mcp_names: set[str] = set()
        for tool in server.tools:
            self._validate_tool(tool)
            if tool.tool_id in seen_tool_ids:
                raise ValidationError(f"duplicate MCP tool_id: {tool.tool_id}")
            if tool.mcp_name in seen_mcp_names:
                raise ValidationError(f"duplicate MCP mcp_name: {tool.mcp_name}")
            seen_tool_ids.add(tool.tool_id)
            seen_mcp_names.add(tool.mcp_name)
        self._validate_json_value(server.metadata, "metadata")

    def _validate_stdio(self, stdio: McpStdioTransportSpec | None) -> None:
        if stdio is None:
            raise ValidationError("MCP stdio transport requires stdio configuration")
        command = stdio.command.strip()
        if not command:
            raise ValidationError("MCP stdio command must be non-empty")
        if command != stdio.command or any(char.isspace() for char in command) or any(char in command for char in "\r\n;&|<>"):
            raise ValidationError("MCP stdio command must be a single argv token, not a shell string")
        for arg in stdio.args:
            if not isinstance(arg, str) or "\x00" in arg:
                raise ValidationError("MCP stdio args must be strings without NUL bytes")
        for child_name, host_name in stdio.env.items():
            self._validate_env_name(child_name, "stdio env name")
            self._validate_env_name(host_name, "stdio env source")
            if not self._env_allowed(host_name, self.config.mcp.stdio_env_allowlist):
                raise ValidationError(f"MCP stdio env source is not allowlisted: {host_name}")
        if stdio.cwd is not None:
            raw = stdio.cwd.replace("\\", "/").strip()
            if not raw or PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
                raise ValidationError("MCP stdio cwd must be a non-empty relative path")
            parts: list[str] = []
            for part in raw.split("/"):
                if part in {"", "."}:
                    continue
                if part == "..":
                    if not parts:
                        raise ValidationError("MCP stdio cwd escapes workspace root")
                    parts.pop()
                    continue
                parts.append(part)

    def _validate_http(self, http: McpHttpTransportSpec | None) -> None:
        if http is None:
            raise ValidationError("MCP streamable_http transport requires http configuration")
        self._validate_url(http.url)
        for name, header in http.headers.items():
            self._validate_header_name(name)
            self._validate_env_name(header.env, f"header {name} env")
            if not self._env_allowed(header.env, self.config.mcp.header_env_allowlist):
                raise ValidationError(f"MCP header env is not allowlisted: {header.env}")
            if header.prefix not in _ALLOWED_HEADER_PREFIXES:
                raise ValidationError(f"MCP header {name} prefix is not allowed")
            if header.suffix not in _ALLOWED_HEADER_SUFFIXES:
                raise ValidationError(f"MCP header {name} suffix is not allowed")

    def _validate_tool(self, tool: McpToolSpec) -> None:
        self._validate_identifier(tool.tool_id, "tool_id", self.config.mcp.tool_id_max_chars)
        if not tool.mcp_name or len(tool.mcp_name) > self.config.mcp.mcp_name_max_chars:
            raise ValidationError("MCP mcp_name must be non-empty and within configured length")
        if tool.right not in _CALL_RIGHTS:
            raise ValidationError("MCP tool right must be read, write, or execute")
        try:
            rollback_class = ExternalEffectRollbackClass(tool.rollback_class)
        except ValueError as exc:
            raise ValidationError("MCP rollback_class is invalid") from exc
        if tool.rollback_status is not None:
            try:
                ExternalEffectRollbackStatus(tool.rollback_status)
            except ValueError as exc:
                raise ValidationError("MCP rollback_status is invalid") from exc
        if rollback_class == ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED and tool.rollback_status is None:
            pass
        self._validate_json_schema(tool.input_schema, "input_schema")
        self._validate_json_value(tool.metadata, "tool metadata")

    def _header_specs(self, value: Any) -> dict[str, McpHeaderSpec]:
        if not isinstance(value, dict):
            raise ValidationError("MCP headers must be an object")
        headers: dict[str, McpHeaderSpec] = {}
        for name, spec in value.items():
            if not isinstance(spec, dict):
                raise ValidationError(f"MCP header {name} must be an object")
            headers[str(name)] = McpHeaderSpec(
                env=str(self._required(spec, "env", f"MCP header {name}")),
                prefix=str(spec.get("prefix", "")),
                suffix=str(spec.get("suffix", "")),
            )
        return headers

    def _required(self, value: dict[str, Any], key: str, context: str) -> Any:
        if key not in value:
            raise ValidationError(f"{context} requires {key}")
        return value[key]

    def _validate_header_name(self, name: str) -> None:
        lowered = name.lower()
        if len(name) > self.config.mcp.header_name_max_chars or not _HEADER_PATTERN.match(name):
            raise ValidationError(f"invalid MCP header name: {name!r}")
        if lowered in _FORBIDDEN_HEADERS:
            raise ValidationError(f"MCP header is forbidden: {name}")

    def _env_allowed(self, name: str, patterns: tuple[str, ...]) -> bool:
        for pattern in patterns:
            if pattern.endswith("*") and name.startswith(pattern[:-1]):
                return True
            if name == pattern:
                return True
        return False

    def _require_runtime_environment(self, server: McpServerSpec) -> None:
        if server.transport == "stdio" and server.stdio is not None:
            for child_name, host_name in server.stdio.env.items():
                resolved = os.environ.get(host_name)
                if resolved is None:
                    raise ValidationError(f"missing environment variable for MCP stdio env {child_name}: {host_name}")
                if "\x00" in resolved:
                    raise ValidationError(f"MCP stdio env {child_name} contains NUL byte")
        if server.transport == "streamable_http" and server.http is not None:
            for name, header in server.http.headers.items():
                resolved = os.environ.get(header.env)
                if resolved is None:
                    raise ValidationError(f"missing environment variable for MCP header {name}: {header.env}")
                header_value = f"{header.prefix}{resolved}{header.suffix}"
                if len(header_value) > self.config.mcp.header_value_max_chars or "\r" in header_value or "\n" in header_value:
                    raise ValidationError(f"MCP header {name} resolved value is invalid")

    def _validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValidationError("MCP HTTP URL must use http or https")
        if parsed.username or parsed.password:
            raise ValidationError("MCP HTTP URL must not include userinfo")
        if parsed.fragment:
            raise ValidationError("MCP HTTP URL must not include a fragment")
        host = parsed.hostname
        if not host:
            raise ValidationError("MCP HTTP URL must include a host")
        if host.lower() in _FORBIDDEN_MCP_HOSTS:
            raise ValidationError("MCP HTTP host is not allowed")
        if parsed.scheme == "http" and host not in _LOCAL_HTTP_HOSTS:
            raise ValidationError("MCP plain HTTP is allowed only for local development hosts")
        self._validate_host_literal(host, allow_local=host in _LOCAL_HTTP_HOSTS)

    def _validate_runtime_resolution(self, server: McpServerSpec) -> tuple[str, ...]:
        if server.http is None:
            return ()
        parsed = urlsplit(server.http.url)
        host = parsed.hostname
        if not host:
            raise ValidationError("MCP HTTP URL must include a host")
        if host in _LOCAL_HTTP_HOSTS:
            return ()
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValidationError(f"MCP host could not be resolved: {host}") from exc
        addresses = sorted({info[4][0] for info in infos})
        if not addresses:
            raise ValidationError(f"MCP host resolved no addresses: {host}")
        for address in addresses:
            self._validate_host_literal(address, allow_local=False)
        return tuple(addresses)

    def _validate_host_literal(self, host: str, *, allow_local: bool) -> None:
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            return
        if allow_local:
            return
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValidationError("MCP HTTP IP address is not allowed")

    def _disable_replaced_server_tool_capabilities(self, server_id: str, *, actor: str) -> None:
        prefix = f"mcp:{server_id}:"
        for cap in self.store.list_capabilities():
            if cap.resource == f"mcp:{server_id}:*" or cap.resource.startswith(prefix):
                self.capabilities.revoke(
                    cap.cap_id,
                    revoked_by=actor,
                    reason="MCP server spec replaced; tool authority must be reissued",
                )

    def _validate_identifier(self, value: str, field: str, max_chars: int) -> None:
        if not isinstance(value, str) or not value or len(value) > max_chars or not _ID_PATTERN.match(value):
            raise ValidationError(f"invalid MCP {field}: {value!r}")

    def _validate_env_name(self, value: str, field: str) -> None:
        if not value or not _ENV_PATTERN.match(value):
            raise ValidationError(f"invalid MCP {field}: {value!r}")

    def _coerce_bool(self, value: Any, field: str) -> bool:
        if not isinstance(value, bool):
            raise ValidationError(f"MCP {field} must be a boolean")
        return value

    def _coerce_positive_float(self, value: Any, field: str) -> float:
        try:
            selected = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"MCP {field} must be a number") from exc
        if not math.isfinite(selected) or selected <= 0:
            raise ValidationError(f"MCP {field} must be > 0")
        return selected

    def _coerce_positive_int(self, value: Any, field: str) -> int:
        if isinstance(value, bool):
            raise ValidationError(f"MCP {field} must be an integer")
        try:
            selected = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"MCP {field} must be an integer") from exc
        if selected <= 0:
            raise ValidationError(f"MCP {field} must be > 0")
        return selected

    def _validate_json_value(self, value: Any, field: str) -> None:
        try:
            dumps(value)
        except Exception as exc:
            raise ValidationError(f"MCP {field} must be JSON-serializable") from exc

    def _validate_json_schema(self, schema: dict[str, Any], field: str) -> None:
        if not schema:
            return
        try:
            jsonschema_validator_for(schema).check_schema(schema)
        except JsonSchemaSchemaError as exc:
            raise ValidationError(f"MCP {field} is not a valid JSON Schema") from exc

    def _validate_arguments_against_schema(self, tool: McpToolSpec, arguments: dict[str, Any]) -> None:
        if not tool.input_schema:
            return
        try:
            validator = jsonschema_validator_for(tool.input_schema)
            validator.check_schema(tool.input_schema)
            validator(tool.input_schema).validate(arguments)
        except JsonSchemaValidationError as exc:
            raise ValidationError(f"MCP tool arguments failed schema validation: {exc.message}") from exc
        except JsonSchemaSchemaError as exc:
            raise ValidationError("MCP tool input_schema is invalid") from exc

    def _bounded_list_limit(self, limit: int | None) -> int:
        selected = self.config.mcp.list_limit if limit is None else limit
        if not isinstance(selected, int):
            raise ValidationError("MCP server list limit must be an integer")
        if selected < 1:
            raise ValidationError("MCP server list limit must be >= 1")
        if selected > self.config.mcp.list_limit:
            raise ValidationError(f"MCP server list limit exceeds configured maximum {self.config.mcp.list_limit}")
        return selected

    def _load_server(self, server_id: str) -> tuple[McpServerSpec, dict[str, Any]]:
        self._validate_identifier(server_id, "server_id", self.config.mcp.server_id_max_chars)
        found = self.store.get_mcp_server(server_id)
        if found is None:
            raise NotFound(f"MCP server not found: {server_id}")
        spec, metadata = found
        self._validate_server(spec)
        return spec, metadata

    def _server_to_json(
        self,
        server: McpServerSpec,
        metadata: dict[str, Any],
        *,
        include_sensitive_fields: bool,
    ) -> dict[str, Any]:
        transport: dict[str, Any]
        if server.transport == "stdio" and server.stdio is not None:
            transport = {
                "type": "stdio",
                "command": server.stdio.command,
                "args": list(server.stdio.args),
                "env": {name: {"env": host_name} for name, host_name in server.stdio.env.items()},
                "cwd": server.stdio.cwd,
            }
        elif server.http is not None:
            transport = {
                "type": "streamable_http",
                "url": server.http.url if include_sensitive_fields else None,
                "headers": {
                    name: {
                        "env": header.env,
                        "prefix": header.prefix,
                        "suffix": header.suffix,
                    }
                    for name, header in server.http.headers.items()
                },
            }
        else:
            transport = {"type": server.transport}
        return {
            "schema_version": server.schema_version,
            "server_id": server.server_id,
            "transport": transport,
            "tools": [self._tool_to_json(server.server_id, tool) for tool in server.tools],
            "timeout_s": server.timeout_s,
            "max_request_bytes": server.max_request_bytes,
            "max_response_bytes": server.max_response_bytes,
            "metadata": server.metadata,
            **metadata,
        }

    def _tool_to_json(self, server_id: str, tool: McpToolSpec, *, live: McpProviderTool | None = None) -> dict[str, Any]:
        payload = {
            "tool_id": tool.tool_id,
            "mcp_name": tool.mcp_name,
            "right": tool.right,
            "resource": self.tool_resource(server_id, tool.tool_id),
            "rollback_class": tool.rollback_class,
            "rollback_status": tool.rollback_status,
            "state_mutation": tool.state_mutation,
            "information_flow": tool.information_flow,
            "input_schema": tool.input_schema,
            "metadata": tool.metadata,
        }
        if live is not None:
            payload["live"] = {
                "name": live.name,
                "description": live.description,
                "input_schema": live.input_schema,
                "schema_matches_manifest": not tool.input_schema or live.input_schema == tool.input_schema,
            }
        return payload

    def _profile_json(self, profile: Any) -> dict[str, Any]:
        return to_jsonable(profile)

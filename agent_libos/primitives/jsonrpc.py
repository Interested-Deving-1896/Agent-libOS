from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

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
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    JsonRpcCallResult,
    JsonRpcCallStatus,
    JsonRpcEndpointSpec,
    JsonRpcHeaderSpec,
    JsonRpcMethodSpec,
    JsonRpcTransportResult,
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
from agent_libos.storage import SQLiteStore
from agent_libos.substrate import JsonRpcProvider
from agent_libos.utils.ids import new_id, utc_now
from agent_libos.utils.serde import dumps, to_jsonable
from agent_libos.utils.yaml_loader import load_yaml_mapping

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]*$")
_ENV_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_FORBIDDEN_HEADERS = {"connection", "content-length", "host", "transfer-encoding", "upgrade"}
_LOCAL_HTTP_HOSTS = {"localhost", "127.0.0.1", "::1"}
_FORBIDDEN_JSONRPC_HOSTS = {"metadata.google.internal"}
_CALL_RIGHTS = {CapabilityRight.READ.value, CapabilityRight.WRITE.value, CapabilityRight.EXECUTE.value}


class JsonRpcPrimitive:
    """Capability-controlled JSON-RPC 2.0 over HTTP client primitive."""

    def __init__(
        self,
        store: SQLiteStore,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        *,
        human: HumanObjectManager | None,
        provider: JsonRpcProvider,
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

    def endpoint_resource(self, endpoint_id: str) -> str:
        return f"jsonrpc_endpoint:{endpoint_id}"

    def method_resource(self, endpoint_id: str, method_id: str) -> str:
        return f"jsonrpc:{endpoint_id}:{method_id}"

    def register_endpoint(
        self,
        endpoint: JsonRpcEndpointSpec | dict[str, Any],
        *,
        actor: str = "runtime",
        replace: bool = False,
        require_capability: bool = True,
        source: str | None = None,
    ) -> dict[str, Any]:
        spec = self._coerce_endpoint(endpoint)
        existing = self.store.get_jsonrpc_endpoint(spec.endpoint_id)
        if existing is not None and not replace:
            raise ValidationError(f"JSON-RPC endpoint already exists: {spec.endpoint_id}")
        if require_capability:
            self.capabilities.require(actor, self.endpoint_resource(spec.endpoint_id), CapabilityRight.WRITE)
        now = utc_now()
        self.store.upsert_jsonrpc_endpoint(spec, registered_by=actor, created_at=now)
        self.events.emit(
            EventType.EXTERNAL_WRITE,
            source=actor,
            target=self.endpoint_resource(spec.endpoint_id),
            payload={"adapter": "jsonrpc", "operation": "endpoint_register", "endpoint_id": spec.endpoint_id},
        )
        self.audit.record(
            actor=actor,
            action="jsonrpc.endpoint.register" if existing is None else "jsonrpc.endpoint.replace",
            target=self.endpoint_resource(spec.endpoint_id),
            decision={
                "endpoint_id": spec.endpoint_id,
                "methods": [method.method_id for method in spec.methods],
                "replaced": existing is not None,
                "source": source,
            },
        )
        return self.inspect_endpoint(spec.endpoint_id, actor=actor, require_capability=False)

    def register_endpoint_from_yaml_text(
        self,
        text: str,
        *,
        actor: str,
        replace: bool = False,
        require_capability: bool = True,
        source: str | None = None,
    ) -> dict[str, Any]:
        if len(text.encode("utf-8")) > self.config.jsonrpc.manifest_max_bytes:
            raise ValidationError(f"JSON-RPC manifest exceeds manifest_max_bytes={self.config.jsonrpc.manifest_max_bytes}")
        data = load_yaml_mapping(text)
        if set(data) == {"jsonrpc_endpoint"} and isinstance(data["jsonrpc_endpoint"], dict):
            data = data["jsonrpc_endpoint"]
        if set(data) == {"endpoint"} and isinstance(data["endpoint"], dict):
            data = data["endpoint"]
        return self.register_endpoint(
            data,
            actor=actor,
            replace=replace,
            require_capability=require_capability,
            source=source,
        )

    def list_endpoints(
        self,
        *,
        actor: str | None = None,
        require_capability: bool = True,
        text: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if require_capability and actor is not None:
            self.capabilities.require(actor, self.config.jsonrpc.registry_resource, CapabilityRight.READ)
        selected_limit = self.config.jsonrpc.list_limit if limit is None else limit
        return [
            self._endpoint_to_json(spec, metadata, include_url=False)
            for spec, metadata in self.store.list_jsonrpc_endpoints(text=text, limit=selected_limit)
        ]

    def inspect_endpoint(
        self,
        endpoint_id: str,
        *,
        actor: str | None = None,
        require_capability: bool = True,
    ) -> dict[str, Any]:
        spec, metadata = self._load_endpoint(endpoint_id)
        if require_capability and actor is not None:
            self.capabilities.require(actor, self.endpoint_resource(endpoint_id), CapabilityRight.READ)
        return self._endpoint_to_json(spec, metadata, include_url=not require_capability)

    def unregister_endpoint(
        self,
        endpoint_id: str,
        *,
        actor: str = "runtime",
        require_capability: bool = True,
    ) -> dict[str, Any]:
        self._load_endpoint(endpoint_id)
        if require_capability:
            self.capabilities.require(actor, self.endpoint_resource(endpoint_id), CapabilityRight.ADMIN)
        self.store.delete_jsonrpc_endpoint(endpoint_id)
        self.audit.record(
            actor=actor,
            action="jsonrpc.endpoint.unregister",
            target=self.endpoint_resource(endpoint_id),
            decision={"endpoint_id": endpoint_id},
        )
        return {"endpoint_id": endpoint_id, "deleted": True}

    def call(
        self,
        pid: str,
        endpoint_id: str,
        method_id: str,
        params: Any = None,
    ) -> JsonRpcCallResult:
        spec, _metadata = self._load_endpoint(endpoint_id)
        method = spec.method_by_id(method_id)
        if method is None:
            raise NotFound(f"JSON-RPC method not found: {endpoint_id}/{method_id}")
        self._validate_json_value(params, "params")
        resource = self.method_resource(endpoint_id, method_id)
        request_id = new_id("jrpc")
        operation_context = self._operation_context(pid, spec, method, params, request_id=request_id)
        decision = self._authorize_call(pid, resource, method.right, operation_context)
        profile = self.capabilities.profiles.jsonrpc(
            resource=resource,
            effect=decision.effect or CapabilityEffect.DENY,
            endpoint_id=endpoint_id,
            method_id=method_id,
        )
        operation_context.update(
            {
                "capability_ids": list(decision.matched_capability_ids),
                "selected_capability_id": decision.selected_capability_id,
                "sandbox_profile": self._profile_json(profile),
            }
        )
        self._require_header_environment(spec)
        request_body = self._request_body(method, params, request_id)
        if len(request_body) > spec.max_request_bytes:
            raise ValidationError(f"JSON-RPC request exceeds max_request_bytes={spec.max_request_bytes}")
        self._preflight_resource_usage(
            pid,
            ResourceUsage(jsonrpc_request_bytes=len(request_body)),
            source="primitive.jsonrpc.call",
            context={"endpoint_id": endpoint_id, "method_id": method_id, "request_bytes": len(request_body)},
        )
        effect_context = self._effect_context(spec, method, operation_context, request_bytes=len(request_body))
        require_external_effect_classifier(self.provider, "call")
        classify_external_effect(self.provider, "call", effect_context, {"preflight": True})
        attempted = False
        try:
            attempted = True
            started = time.monotonic()
            try:
                transport = self.provider.call(
                    spec,
                    method,
                    request_body,
                    timeout_s=spec.timeout_s,
                    max_response_bytes=spec.max_response_bytes,
                )
            except Exception as exc:
                transport = JsonRpcTransportResult(
                    status_code=None,
                    body=b"",
                    elapsed_s=time.monotonic() - started,
                    response_bytes=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            result = self._call_result_from_transport(spec, method, request_id, transport)
            event = self._emit_call_event(pid, resource, result, method)
            audit_record = self._record_call_audit(pid, resource, result, method, operation_context)
            self._record_external_effect(pid, resource, effect_context, result, event, audit_record)
            self._charge_resource_usage(
                pid,
                ResourceUsage(jsonrpc_request_bytes=len(request_body), jsonrpc_response_bytes=result.response_bytes),
                source="primitive.jsonrpc.call",
                context={
                    "endpoint_id": endpoint_id,
                    "method_id": method_id,
                    "request_bytes": len(request_body),
                    "response_bytes": result.response_bytes,
                    "status": result.status.value,
                },
            )
            return result
        finally:
            if attempted and decision.consume_capability_id is not None:
                self.capabilities.consume_use(
                    decision.consume_capability_id,
                    used_by="jsonrpc",
                    reason="one-time JSON-RPC method permission consumed",
                )

    async def acall(self, pid: str, endpoint_id: str, method_id: str, params: Any = None) -> JsonRpcCallResult:
        return await asyncio.to_thread(self.call, pid, endpoint_id, method_id, params)

    def grant_method(
        self,
        pid: str,
        endpoint_id: str,
        method_id: str,
        *,
        right: str | CapabilityRight,
        issued_by: str = "jsonrpc",
        delegable: bool = True,
    ) -> Any:
        return self.capabilities.grant(
            subject=pid,
            resource=self.method_resource(endpoint_id, method_id),
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
                raise CapabilityDenied(f"{pid} requires human approval for JSON-RPC call on {resource}")
            profile = self.capabilities.profiles.jsonrpc(
                resource=resource,
                effect=CapabilityEffect.ASK,
                endpoint_id=str(context["endpoint_id"]),
                method_id=str(context["method_id"]),
            )
            approval_context = {**context, "sandbox_profile": self._profile_json(profile)}
            request_id = self.human.query(
                pid=pid,
                human=self.config.runtime.default_human,
                request={
                    "type": "external_operation_approval",
                    "question": f"Allow this process to call remote JSON-RPC method {resource}?",
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

    def _request_body(self, method: JsonRpcMethodSpec, params: Any, request_id: str) -> bytes:
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method.rpc_method,
        }
        if params is not None:
            payload["params"] = params
        return dumps(payload).encode("utf-8")

    def _call_result_from_transport(
        self,
        endpoint: JsonRpcEndpointSpec,
        method: JsonRpcMethodSpec,
        request_id: str,
        transport: JsonRpcTransportResult,
    ) -> JsonRpcCallResult:
        if transport.error and transport.status_code is None:
            return self._failure(endpoint, method, request_id, JsonRpcCallStatus.TRANSPORT_ERROR, transport, transport.error)
        if transport.too_large:
            return self._failure(
                endpoint,
                method,
                request_id,
                JsonRpcCallStatus.RESPONSE_TOO_LARGE,
                transport,
                f"response exceeded max_response_bytes={endpoint.max_response_bytes}",
            )
        if transport.status_code is None or not 200 <= transport.status_code < 300:
            return self._failure(
                endpoint,
                method,
                request_id,
                JsonRpcCallStatus.HTTP_ERROR,
                transport,
                "HTTP status was not successful",
                extra={"body_preview": self._decode_preview(transport.body)},
            )
        try:
            envelope = json.loads(transport.body.decode("utf-8"))
        except Exception as exc:
            return self._failure(endpoint, method, request_id, JsonRpcCallStatus.INVALID_RESPONSE, transport, str(exc))
        if not isinstance(envelope, dict):
            return self._failure(endpoint, method, request_id, JsonRpcCallStatus.INVALID_RESPONSE, transport, "response is not a JSON object")
        if envelope.get("jsonrpc") != "2.0":
            return self._failure(endpoint, method, request_id, JsonRpcCallStatus.INVALID_RESPONSE, transport, "missing jsonrpc=2.0")
        if envelope.get("id") != request_id:
            return self._failure(endpoint, method, request_id, JsonRpcCallStatus.INVALID_RESPONSE, transport, "response id mismatch")
        has_result = "result" in envelope
        has_error = "error" in envelope
        if has_result == has_error:
            return self._failure(
                endpoint,
                method,
                request_id,
                JsonRpcCallStatus.INVALID_RESPONSE,
                transport,
                "response must contain exactly one of result or error",
            )
        if has_error:
            error = envelope["error"] if isinstance(envelope["error"], dict) else {"message": str(envelope["error"])}
            return JsonRpcCallResult(
                endpoint_id=endpoint.endpoint_id,
                method_id=method.method_id,
                rpc_method=method.rpc_method,
                request_id=request_id,
                status=JsonRpcCallStatus.JSONRPC_ERROR,
                http_status=transport.status_code,
                ok=False,
                error=to_jsonable(error),
                response_bytes=transport.response_bytes,
                duration_s=transport.elapsed_s,
            )
        return JsonRpcCallResult(
            endpoint_id=endpoint.endpoint_id,
            method_id=method.method_id,
            rpc_method=method.rpc_method,
            request_id=request_id,
            status=JsonRpcCallStatus.OK,
            http_status=transport.status_code,
            ok=True,
            result=to_jsonable(envelope.get("result")),
            response_bytes=transport.response_bytes,
            duration_s=transport.elapsed_s,
        )

    def _failure(
        self,
        endpoint: JsonRpcEndpointSpec,
        method: JsonRpcMethodSpec,
        request_id: str,
        status: JsonRpcCallStatus,
        transport: JsonRpcTransportResult,
        message: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> JsonRpcCallResult:
        return JsonRpcCallResult(
            endpoint_id=endpoint.endpoint_id,
            method_id=method.method_id,
            rpc_method=method.rpc_method,
            request_id=request_id,
            status=status,
            http_status=transport.status_code,
            ok=False,
            error={"message": message, **dict(extra or {})},
            response_bytes=transport.response_bytes,
            duration_s=transport.elapsed_s,
        )

    def _emit_call_event(
        self,
        pid: str,
        resource: str,
        result: JsonRpcCallResult,
        method: JsonRpcMethodSpec,
    ) -> Event:
        event_type = EventType.EXTERNAL_WRITE if method.state_mutation or method.right != CapabilityRight.READ.value else EventType.EXTERNAL_READ
        return self.events.emit(
            event_type,
            source=pid,
            target=resource,
            payload={
                "adapter": "jsonrpc",
                "endpoint_id": result.endpoint_id,
                "method_id": result.method_id,
                "status": result.status.value,
                "ok": result.ok,
                "http_status": result.http_status,
                "response_bytes": result.response_bytes,
                "duration_s": result.duration_s,
            },
        )

    def _record_call_audit(
        self,
        pid: str,
        resource: str,
        result: JsonRpcCallResult,
        method: JsonRpcMethodSpec,
        operation_context: dict[str, Any],
    ) -> AuditRecord:
        return self.audit.record(
            actor=pid,
            action="primitive.jsonrpc.call",
            target=resource,
            decision={
                "endpoint_id": result.endpoint_id,
                "method_id": result.method_id,
                "rpc_method": method.rpc_method,
                "right": method.right,
                "request_id": result.request_id,
                "params_sha256": operation_context["params_sha256"],
                "params_preview": operation_context["params_preview"],
                "sandbox_profile": operation_context.get("sandbox_profile"),
                "status": result.status.value,
                "ok": result.ok,
                "http_status": result.http_status,
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
        result: JsonRpcCallResult,
        event: Event,
        audit_record: AuditRecord,
    ) -> None:
        result_payload = {
            "status": result.status.value,
            "ok": result.ok,
            "http_status": result.http_status,
            "response_bytes": result.response_bytes,
            "duration_s": result.duration_s,
        }
        classification = classify_external_effect(self.provider, "call", context, result_payload)
        record_external_effect(
            self.store,
            pid=pid,
            provider="jsonrpc",
            operation="call",
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
        endpoint: JsonRpcEndpointSpec,
        method: JsonRpcMethodSpec,
        params: Any,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        params_json = dumps(params)
        return {
            "pid": pid,
            "primitive": "runtime.jsonrpc.call",
            "operation": "jsonrpc.call",
            "authority_operation": "jsonrpc.call",
            "endpoint_id": endpoint.endpoint_id,
            "method_id": method.method_id,
            "rpc_method": method.rpc_method,
            "right": method.right,
            "request_id": request_id,
            "params_sha256": hashlib.sha256(params_json.encode("utf-8")).hexdigest(),
            "params_preview": self._preview(params_json),
        }

    def _approval_constraints(self, context: dict[str, Any]) -> dict[str, Any]:
        return {
            AUTHORITY_RULES_KEY: [
                {
                    "rule_id": f"jsonrpc.approval.{context['endpoint_id']}.{context['method_id']}",
                    "operation": "jsonrpc.call",
                    "effect": CapabilityEffect.ALLOW.value,
                    "risk": "high",
                    "conditions": {
                        "endpoint_id": context["endpoint_id"],
                        "method_id": context["method_id"],
                        "params_sha256": context["params_sha256"],
                    },
                    "description": "one-shot human approval for exact JSON-RPC call payload",
                }
            ]
        }

    def _effect_context(
        self,
        endpoint: JsonRpcEndpointSpec,
        method: JsonRpcMethodSpec,
        operation_context: dict[str, Any],
        *,
        request_bytes: int,
    ) -> dict[str, Any]:
        return {
            **operation_context,
            "request_bytes": request_bytes,
            "endpoint_id": endpoint.endpoint_id,
            "method_id": method.method_id,
            "rpc_method": method.rpc_method,
            "method": {
                "right": method.right,
                "rollback_class": method.rollback_class,
                "rollback_status": method.rollback_status or self._default_rollback_status(method.rollback_class),
                "state_mutation": method.state_mutation,
                "information_flow": method.information_flow,
            },
        }

    def _coerce_endpoint(self, value: JsonRpcEndpointSpec | dict[str, Any]) -> JsonRpcEndpointSpec:
        if isinstance(value, JsonRpcEndpointSpec):
            self._validate_endpoint(value)
            return value
        if not isinstance(value, dict):
            raise ValidationError("JSON-RPC endpoint must be a mapping")
        unknown = sorted(set(value) - {
            "schema_version",
            "endpoint_id",
            "url",
            "headers",
            "methods",
            "timeout_s",
            "max_request_bytes",
            "max_response_bytes",
            "metadata",
        })
        if unknown:
            raise ValidationError(f"unknown JSON-RPC endpoint fields: {unknown}")
        methods = value.get("methods")
        if not isinstance(methods, list) or not methods:
            raise ValidationError("JSON-RPC endpoint requires a non-empty methods list")
        spec = JsonRpcEndpointSpec(
            schema_version=int(value.get("schema_version", 1)),
            endpoint_id=self._require_string(value.get("endpoint_id"), "endpoint_id"),
            url=self._require_string(value.get("url"), "url"),
            headers=self._header_specs(value.get("headers") or {}),
            methods=[self._method_spec(item) for item in methods],
            timeout_s=float(value.get("timeout_s", self.config.jsonrpc.timeout_s)),
            max_request_bytes=int(value.get("max_request_bytes", self.config.jsonrpc.max_request_bytes)),
            max_response_bytes=int(value.get("max_response_bytes", self.config.jsonrpc.max_response_bytes)),
            metadata=dict(value.get("metadata") or {}),
        )
        self._validate_endpoint(spec)
        return spec

    def _validate_endpoint(self, endpoint: JsonRpcEndpointSpec) -> None:
        if endpoint.schema_version != 1:
            raise ValidationError("JSON-RPC endpoint schema_version must be 1")
        self._validate_identifier(endpoint.endpoint_id, "endpoint_id", self.config.jsonrpc.endpoint_id_max_chars)
        self._validate_url(endpoint.url)
        if not 0 < endpoint.timeout_s <= self.config.jsonrpc.timeout_hard_limit_s:
            raise ValidationError("JSON-RPC endpoint timeout_s exceeds configured bounds")
        if not 0 < endpoint.max_request_bytes <= self.config.jsonrpc.max_request_hard_limit_bytes:
            raise ValidationError("JSON-RPC endpoint max_request_bytes exceeds configured bounds")
        if not 0 < endpoint.max_response_bytes <= self.config.jsonrpc.max_response_hard_limit_bytes:
            raise ValidationError("JSON-RPC endpoint max_response_bytes exceeds configured bounds")
        seen: set[str] = set()
        for name, header in endpoint.headers.items():
            self._validate_header_name(name)
            self._validate_header_value_part(header.env, "header env")
            self._validate_header_value_part(header.prefix, "header prefix")
            self._validate_header_value_part(header.suffix, "header suffix")
        for method in endpoint.methods:
            self._validate_method(method)
            if method.method_id in seen:
                raise ValidationError(f"duplicate JSON-RPC method_id: {method.method_id}")
            seen.add(method.method_id)

    def _method_spec(self, value: Any) -> JsonRpcMethodSpec:
        if not isinstance(value, dict):
            raise ValidationError("JSON-RPC method entries must be mappings")
        unknown = sorted(set(value) - {
            "method_id",
            "rpc_method",
            "right",
            "rollback_class",
            "rollback_status",
            "state_mutation",
            "information_flow",
            "params_schema",
            "metadata",
        })
        if unknown:
            raise ValidationError(f"unknown JSON-RPC method fields: {unknown}")
        return JsonRpcMethodSpec(
            method_id=self._require_string(value.get("method_id"), "method_id"),
            rpc_method=self._require_string(value.get("rpc_method"), "rpc_method"),
            right=self._require_string(value.get("right"), "right"),
            rollback_class=self._require_string(value.get("rollback_class"), "rollback_class"),
            rollback_status=str(value["rollback_status"]) if value.get("rollback_status") is not None else None,
            state_mutation=self._require_bool(value.get("state_mutation"), "state_mutation"),
            information_flow=self._require_bool(value.get("information_flow"), "information_flow"),
            params_schema=dict(value.get("params_schema") or {}),
            metadata=dict(value.get("metadata") or {}),
        )

    def _validate_method(self, method: JsonRpcMethodSpec) -> None:
        self._validate_identifier(method.method_id, "method_id", self.config.jsonrpc.method_id_max_chars)
        if not method.rpc_method or len(method.rpc_method) > self.config.jsonrpc.rpc_method_max_chars:
            raise ValidationError("JSON-RPC rpc_method is empty or too long")
        if any(ord(char) < 32 for char in method.rpc_method):
            raise ValidationError("JSON-RPC rpc_method contains control characters")
        if method.right not in _CALL_RIGHTS:
            raise ValidationError("JSON-RPC method right must be read, write, or execute")
        try:
            rollback_class = ExternalEffectRollbackClass(method.rollback_class)
            rollback_status = method.rollback_status or self._default_rollback_status(method.rollback_class)
            ExternalEffectRollbackStatus(rollback_status)
        except ValueError as exc:
            raise ValidationError("JSON-RPC method has invalid rollback_class or rollback_status") from exc
        if rollback_class == ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED and bool(method.state_mutation):
            raise ValidationError("no_rollback_required JSON-RPC methods cannot declare state_mutation=true")

    def _header_specs(self, value: Any) -> dict[str, JsonRpcHeaderSpec]:
        if not isinstance(value, dict):
            raise ValidationError("JSON-RPC headers must be a mapping")
        headers: dict[str, JsonRpcHeaderSpec] = {}
        for raw_name, raw_spec in value.items():
            name = str(raw_name)
            if not isinstance(raw_spec, dict) or "env" not in raw_spec:
                raise ValidationError("JSON-RPC headers must use env-backed mappings")
            unknown = sorted(set(raw_spec) - {"env", "prefix", "suffix"})
            if unknown:
                raise ValidationError(f"unknown JSON-RPC header fields for {name}: {unknown}")
            headers[name] = JsonRpcHeaderSpec(
                env=self._require_string(raw_spec.get("env"), f"headers.{name}.env"),
                prefix=str(raw_spec.get("prefix", "")),
                suffix=str(raw_spec.get("suffix", "")),
            )
        return headers

    def _validate_header_name(self, name: str) -> None:
        if len(name) > self.config.jsonrpc.header_name_max_chars or not _HEADER_PATTERN.match(name):
            raise ValidationError(f"invalid JSON-RPC header name: {name!r}")
        if name.lower() in _FORBIDDEN_HEADERS:
            raise ValidationError(f"JSON-RPC header cannot be configured by endpoint manifest: {name}")

    def _validate_header_value_part(self, value: str, field: str) -> None:
        if len(value) > self.config.jsonrpc.header_value_max_chars:
            raise ValidationError(f"JSON-RPC {field} exceeds configured maximum length")
        if "\r" in value or "\n" in value:
            raise ValidationError(f"JSON-RPC {field} contains a newline")
        if field == "header env" and not _ENV_PATTERN.match(value):
            raise ValidationError(f"JSON-RPC header env is not a valid environment variable name: {value!r}")

    def _require_header_environment(self, endpoint: JsonRpcEndpointSpec) -> None:
        missing = sorted(spec.env for spec in endpoint.headers.values() if os.environ.get(spec.env) is None)
        if missing:
            raise ValidationError(f"missing JSON-RPC header environment variables: {missing}")

    def _validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            raise ValidationError("JSON-RPC endpoint URL must not include userinfo")
        if parsed.fragment:
            raise ValidationError("JSON-RPC endpoint URL must not include a fragment")
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValidationError("JSON-RPC endpoint URL must be HTTP(S)")
        host = (parsed.hostname or "").rstrip(".").lower()
        self._validate_remote_host(host)
        if parsed.scheme == "http" and host not in _LOCAL_HTTP_HOSTS:
            raise ValidationError("plain HTTP JSON-RPC endpoints are restricted to localhost")

    def _validate_remote_host(self, host: str) -> None:
        if host in _FORBIDDEN_JSONRPC_HOSTS:
            raise ValidationError("JSON-RPC endpoint host is blocked")
        try:
            address = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            return
        if address.is_loopback:
            return
        if (
            address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValidationError("JSON-RPC endpoint IP address is not allowed")

    def _validate_identifier(self, value: str, field: str, max_chars: int) -> None:
        if len(value) > max_chars or not _ID_PATTERN.match(value):
            raise ValidationError(f"invalid JSON-RPC {field}: {value!r}")

    def _require_string(self, value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"JSON-RPC {field} must be a non-empty string")
        return value.strip()

    def _require_bool(self, value: Any, field: str) -> bool:
        if not isinstance(value, bool):
            raise ValidationError(f"JSON-RPC {field} must be a boolean")
        return value

    def _validate_json_value(self, value: Any, field: str) -> None:
        try:
            dumps(value)
        except Exception as exc:
            raise ValidationError(f"JSON-RPC {field} must be JSON-serializable") from exc

    def _load_endpoint(self, endpoint_id: str) -> tuple[JsonRpcEndpointSpec, dict[str, Any]]:
        self._validate_identifier(endpoint_id, "endpoint_id", self.config.jsonrpc.endpoint_id_max_chars)
        found = self.store.get_jsonrpc_endpoint(endpoint_id)
        if found is None:
            raise NotFound(f"JSON-RPC endpoint not found: {endpoint_id}")
        return found

    def _endpoint_to_json(
        self,
        endpoint: JsonRpcEndpointSpec,
        metadata: dict[str, Any],
        *,
        include_url: bool,
    ) -> dict[str, Any]:
        return {
            "schema_version": endpoint.schema_version,
            "endpoint_id": endpoint.endpoint_id,
            "url": endpoint.url if include_url else None,
            "headers": {
                name: {"env": spec.env, "prefix": spec.prefix, "suffix": spec.suffix, "value": "<redacted>"}
                for name, spec in endpoint.headers.items()
            },
            "methods": [
                {
                    "method_id": method.method_id,
                    "rpc_method": method.rpc_method,
                    "right": method.right,
                    "resource": self.method_resource(endpoint.endpoint_id, method.method_id),
                    "rollback_class": method.rollback_class,
                    "rollback_status": method.rollback_status or self._default_rollback_status(method.rollback_class),
                    "state_mutation": method.state_mutation,
                    "information_flow": method.information_flow,
                    "params_schema": method.params_schema,
                    "metadata": method.metadata,
                }
                for method in endpoint.methods
            ],
            "metadata": endpoint.metadata,
            "registered_by": metadata.get("registered_by"),
            "created_at": metadata.get("created_at"),
            "updated_at": metadata.get("updated_at"),
        }

    def _default_rollback_status(self, rollback_class: str) -> str:
        selected = ExternalEffectRollbackClass(rollback_class)
        if selected == ExternalEffectRollbackClass.ROLLBACKABLE:
            return ExternalEffectRollbackStatus.NOT_APPLIED.value
        if selected == ExternalEffectRollbackClass.IRREVERSIBLE:
            return ExternalEffectRollbackStatus.NOT_SUPPORTED.value
        return ExternalEffectRollbackStatus.NOT_REQUIRED.value

    def _profile_json(self, profile: Any) -> dict[str, Any]:
        return {
            "operation": profile.operation,
            "resource": profile.resource,
            "effect": profile.effect.value,
            "risk": profile.risk.value,
            "rule_id": profile.rule_id,
            "restrictions": dict(profile.restrictions),
        }

    def _preview(self, text: str) -> str:
        limit = self.config.jsonrpc.audit_preview_chars
        return text if len(text) <= limit else f"{text[:limit]}..."

    def _decode_preview(self, value: bytes) -> str:
        return self._preview(value.decode("utf-8", errors="replace"))

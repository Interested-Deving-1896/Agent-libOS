from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from agent_libos import Runtime
from agent_libos.models import (
    CapabilityStatus,
    CapabilityRight,
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    McpProviderCallResult,
    McpProviderTool,
    McpToolListResult,
)
from agent_libos.models.exceptions import CapabilityDenied, NotFound, ValidationError
from agent_libos.runtime.syscalls import LibOSSyscallSession
from agent_libos.substrate.local import _allowed_mcp_connect_addresses
from agent_libos.utils.serde import dumps


class TestMcpPrimitive:
    def test_manifest_validation_rejects_unsafe_server_shapes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("valid"), actor="cli", require_capability=False)
            invalid_cases = [
                _manifest_without_server_id(),
                _stdio_manifest("bad:colon"),
                _stdio_manifest("shell-string", command="python server.py"),
                _stdio_manifest("dup-tool", duplicate_tool=True),
                _stdio_manifest("bad-env", env_source="OPENAI_API_KEY"),
                _stdio_manifest("bad-cwd", cwd="../outside"),
                _http_manifest("bad-http", "http://api.example.test/mcp"),
                _http_manifest("bad-userinfo", "https://user:pass@example.test/mcp"),
                _http_manifest("bad-fragment", "https://api.example.test/mcp#secret"),
                _http_manifest("bad-private-ip", "https://10.0.0.10/mcp"),
                _http_manifest("bad-nonpublic-ip", "https://100.64.0.1/mcp"),
                _http_manifest("literal-header", "https://api.example.test/mcp", literal_header=True),
                _http_manifest("bad-header-env", "https://api.example.test/mcp", header_env="OPENAI_API_KEY"),
                _stdio_manifest("bad-effect", state_mutation=True),
            ]
            monkeypatch.setenv("AGENT_LIBOS_MCP_TEST_TOKEN", "token")
            for text in invalid_cases:
                with pytest.raises(ValidationError):
                    runtime.mcp.register_server_from_yaml_text(text, actor="cli", require_capability=False)
        finally:
            runtime.close()

    def test_call_requires_tool_capability_and_records_effect(self) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp call")
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("demo"), actor="cli", require_capability=False)

            with pytest.raises(CapabilityDenied):
                runtime.mcp.call_tool(pid, "demo", "echo", {"text": "hello"})

            runtime.capability.grant(pid, "mcp:demo:echo", [CapabilityRight.READ], issued_by="test")
            result = runtime.mcp.call_tool(pid, "demo", "echo", {"text": "hello"})

            assert result.ok
            assert result.result["structured_content"] == {"echo": {"text": "hello"}}
            assert provider.list_calls == ["demo"]
            assert provider.call_args == [("demo", "echo", {"text": "hello"})]
            process = runtime.process.get(pid)
            assert process.resource_usage.mcp_request_bytes > 0
            assert process.resource_usage.mcp_response_bytes > 0
            assert process.resource_usage.jsonrpc_request_bytes == 0
            effect = [item for item in runtime.store.list_external_effects() if item.provider == "mcp"][0]
            assert effect.operation == "call_tool"
            assert effect.rollback_class == ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED
            assert not effect.state_mutation
            assert effect.information_flow
        finally:
            runtime.close()

    def test_live_schema_mismatch_consumes_and_records_one_shot_attempt(self) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider(
            live_schema={"type": "object", "properties": {"other": {"type": "string"}}}
        )
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp schema mismatch")
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("demo"), actor="cli", require_capability=False)
            cap = runtime.capability.grant_once(pid, "mcp:demo:echo", [CapabilityRight.READ], issued_by="test")

            with pytest.raises(ValidationError, match="schema changed"):
                runtime.mcp.call_tool(pid, "demo", "echo", {"text": "hello"})

            assert provider.call_args == []
            assert runtime.store.get_capability(cap.cap_id).uses_remaining == 0
            effect = [item for item in runtime.store.list_external_effects() if item.provider == "mcp"][0]
            assert effect.operation == "call_tool"
            assert effect.target == "mcp:demo:echo"
            assert effect.provider_metadata["result"]["ok"] is False
            assert effect.provider_metadata["result"]["status"] == "invalid_response"
        finally:
            runtime.close()

    def test_http_dns_private_resolution_denies_before_provider_or_capability_use(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        monkeypatch.setenv("AGENT_LIBOS_MCP_TEST_TOKEN", "token")

        def fake_getaddrinfo(*_args: Any, **_kwargs: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 443))]

        monkeypatch.setattr("agent_libos.primitives.mcp.socket.getaddrinfo", fake_getaddrinfo)
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp dns")
            runtime.mcp.register_server_from_yaml_text(
                _http_manifest("dns-demo", "https://mcp.example.test/tools"),
                actor="cli",
                require_capability=False,
            )
            cap = runtime.capability.grant_once(pid, "mcp:dns-demo:echo", [CapabilityRight.READ], issued_by="test")

            with pytest.raises(ValidationError, match="IP address is not allowed"):
                runtime.mcp.call_tool(pid, "dns-demo", "echo", {"text": "hello"})

            assert provider.list_calls == []
            assert provider.call_args == []
            assert runtime.store.get_capability(cap.cap_id).uses_remaining == 1
        finally:
            runtime.close()

    def test_provider_connect_policy_rejects_rebound_private_dns(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fake_getaddrinfo(*_args: Any, **_kwargs: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 443))]

        monkeypatch.setattr("agent_libos.substrate.local.socket.getaddrinfo", fake_getaddrinfo)

        with pytest.raises(ValidationError, match="IP address is not allowed"):
            _allowed_mcp_connect_addresses("mcp.example.test", 443)

    def test_list_tools_without_refresh_uses_registered_metadata_only(self) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp metadata list")
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("demo"), actor="cli", require_capability=False)
            runtime.capability.grant(pid, "mcp_server:demo", [CapabilityRight.READ], issued_by="test")

            result = runtime.mcp.list_tools("demo", actor=pid, refresh=False)

            assert result["refreshed"] is False
            assert result["response_bytes"] == 0
            assert provider.list_calls == []
            assert runtime.store.list_external_effects() == []
        finally:
            runtime.close()

    def test_list_tools_refresh_without_process_actor_records_host_effect(self) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        try:
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("demo"), actor="cli", require_capability=False)

            result = runtime.mcp.list_tools("demo", actor=None, require_capability=False, refresh=True)

            assert result["refreshed"] is True
            assert provider.list_calls == ["demo"]
            effect = [item for item in runtime.store.list_external_effects() if item.provider == "mcp"][0]
            assert effect.operation == "list_tools"
            assert effect.pid == "runtime"
            audit = [
                record
                for record in runtime.audit.trace()
                if record.action == "primitive.mcp.list_tools" and record.actor == "runtime"
            ][0]
            assert audit.decision["ok"] is True
            event = [
                item
                for item in runtime.events.list(target="mcp_server:demo")
                if item.payload.get("operation") == "list_tools"
            ][0]
            assert event.source == "runtime"
        finally:
            runtime.close()

    def test_list_tools_refresh_requires_execute_and_records_effect(self) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp live list")
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("demo"), actor="cli", require_capability=False)
            runtime.capability.grant(pid, "mcp_server:demo", [CapabilityRight.READ], issued_by="test")

            with pytest.raises(CapabilityDenied):
                runtime.mcp.list_tools("demo", actor=pid, refresh=True)

            assert provider.list_calls == []
            runtime.capability.grant(pid, "mcp_server:demo", [CapabilityRight.EXECUTE], issued_by="test")
            result = runtime.mcp.list_tools("demo", actor=pid, refresh=True)

            assert result["refreshed"] is True
            assert result["response_bytes"] == 128
            assert provider.list_calls == ["demo"]
            process = runtime.process.get(pid)
            assert process.resource_usage.mcp_request_bytes > 0
            assert process.resource_usage.mcp_response_bytes >= 128
            effect = [item for item in runtime.store.list_external_effects() if item.provider == "mcp"][0]
            assert effect.operation == "list_tools"
            assert effect.target == "mcp_server:demo"
            assert not effect.state_mutation
            assert effect.information_flow
        finally:
            runtime.close()

    def test_list_tools_refresh_provider_failure_records_failed_attempt(self) -> None:
        runtime = Runtime.open("local")
        provider = _FailingListMcpProvider("tools/list failed with token=SECRET_MCP_LIST_TOKEN")
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp failed live list")
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("demo"), actor="cli", require_capability=False)
            runtime.capability.grant(pid, "mcp_server:demo", [CapabilityRight.READ, CapabilityRight.EXECUTE], issued_by="test")

            with pytest.raises(RuntimeError, match="tools/list failed"):
                runtime.mcp.list_tools("demo", actor=pid, refresh=True)

            assert provider.list_calls == ["demo"]
            process = runtime.process.get(pid)
            assert process.resource_usage.mcp_request_bytes > 0
            assert process.resource_usage.mcp_response_bytes == 0
            effect = [item for item in runtime.store.list_external_effects() if item.provider == "mcp"][0]
            assert effect.operation == "list_tools"
            assert effect.target == "mcp_server:demo"
            assert effect.provider_metadata["result"]["ok"] is False
            assert effect.provider_metadata["result"]["status"] == "transport_error"
            audit = [
                record
                for record in runtime.audit.trace()
                if record.action == "primitive.mcp.list_tools" and record.actor == pid
            ][0]
            assert audit.decision["ok"] is False
            event = [
                item
                for item in runtime.events.list(target="mcp_server:demo")
                if item.payload.get("operation") == "list_tools"
            ][0]
            assert event.payload["ok"] is False
            observed = dumps(
                {
                    "audit": audit.decision,
                    "event": event.payload,
                    "effect": effect.provider_metadata,
                }
            )
            assert "SECRET_MCP_LIST_TOKEN" not in observed
            assert "sha256" in observed
        finally:
            runtime.close()

    def test_list_tools_refresh_requires_list_tools_classifier_before_provider_call(self) -> None:
        runtime = Runtime.open("local")
        provider = _CallOnlyClassifierMcpProvider()
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp live classifier")
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("demo"), actor="cli", require_capability=False)
            runtime.capability.grant(pid, "mcp_server:demo", [CapabilityRight.READ, CapabilityRight.EXECUTE], issued_by="test")

            with pytest.raises(ValueError, match="unsupported"):
                runtime.mcp.list_tools("demo", actor=pid, refresh=True)

            assert provider.list_calls == []
            assert runtime.store.list_external_effects() == []
        finally:
            runtime.close()

    def test_syscall_bypasses_tool_table_but_not_capabilities(self) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp syscall")
            process = runtime.process.get(pid)
            process.tool_table = {}
            runtime.store.update_process(process)
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("demo"), actor="cli", require_capability=False)

            session = LibOSSyscallSession(runtime, pid)
            with pytest.raises(CapabilityDenied):
                asyncio.run(session.handle("mcp.call", {"server_id": "demo", "tool_id": "echo", "arguments": {}}))

            runtime.capability.grant(pid, "mcp:demo:echo", [CapabilityRight.READ], issued_by="test")
            result = asyncio.run(
                session.handle("mcp.call", {"server_id": "demo", "tool_id": "echo", "arguments": {"text": "ok"}})
            )

            assert result["ok"]
            assert result["result"]["structured_content"] == {"echo": {"text": "ok"}}
        finally:
            runtime.close()

    def test_replace_with_server_admin_disables_stale_tool_grants(self) -> None:
        runtime = Runtime.open("local")
        try:
            actor = runtime.process.spawn(image="base-agent:v0", goal="mcp admin")
            caller = runtime.process.spawn(image="base-agent:v0", goal="mcp caller")
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("demo"), actor="cli", require_capability=False)
            runtime.capability.grant(actor, "mcp_server:demo", [CapabilityRight.ADMIN], issued_by="test")
            tool_cap = runtime.capability.grant(caller, "mcp:demo:echo", [CapabilityRight.READ], issued_by="test")

            runtime.mcp.register_server_from_yaml_text(
                _stdio_manifest("demo", mcp_name="demo.changed"),
                actor=actor,
                replace=True,
                require_capability=True,
            )

            stored, _metadata = runtime.store.get_mcp_server("demo")
            assert stored.tools[0].mcp_name == "demo.changed"
            assert runtime.store.get_capability(tool_cap.cap_id).status == CapabilityStatus.DISABLED
        finally:
            runtime.close()

    def test_replace_rolls_back_server_spec_when_stale_grant_disable_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime = Runtime.open("local")
        try:
            actor = runtime.process.spawn(image="base-agent:v0", goal="mcp admin")
            caller = runtime.process.spawn(image="base-agent:v0", goal="mcp caller")
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("demo"), actor="cli", require_capability=False)
            runtime.capability.grant(actor, "mcp_server:demo", [CapabilityRight.ADMIN], issued_by="test")
            runtime.capability.grant(caller, "mcp:demo:echo", [CapabilityRight.READ], issued_by="test")

            def fail_disable(*_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("disable failed")

            monkeypatch.setattr(runtime.capability, "disable_subject_capability", fail_disable)
            with pytest.raises(RuntimeError, match="disable failed"):
                runtime.mcp.register_server_from_yaml_text(
                    _stdio_manifest("demo", mcp_name="demo.changed"),
                    actor=actor,
                    replace=True,
                    require_capability=True,
                )

            stored, _metadata = runtime.store.get_mcp_server("demo")
            assert stored.tools[0].mcp_name == "demo.echo"
        finally:
            runtime.close()

    def test_checkpoint_reports_mcp_effect_but_does_not_restore_server_registry(self) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp checkpoint")
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("ckpt"), actor="cli", require_capability=False)
            runtime.capability.grant(pid, "mcp:ckpt:echo", [CapabilityRight.READ], issued_by="test")
            checkpoint_id = runtime.checkpoint.create(pid, "before mcp", actor=pid)
            runtime.mcp.call_tool(pid, "ckpt", "echo", {"text": "after"})
            runtime.mcp.unregister_server("ckpt", actor="cli", require_capability=False)

            with pytest.raises(NotFound):
                runtime.mcp.inspect_server("ckpt", require_capability=False)

            restored = runtime.checkpoint.restore("cli", checkpoint_id, require_capability=False)

            assert restored["external_effect_summary"]["by_provider_operation"]["mcp.call_tool"] == 1
            with pytest.raises(NotFound):
                runtime.mcp.inspect_server("ckpt", require_capability=False)
            with pytest.raises(NotFound):
                runtime.mcp.call_tool(pid, "ckpt", "echo", {"text": "again"})
        finally:
            runtime.close()


def _stdio_manifest(
    server_id: str,
    *,
    command: str = "python3",
    mcp_name: str = "demo.echo",
    duplicate_tool: bool = False,
    env_source: str | None = None,
    cwd: str | None = None,
    state_mutation: bool = False,
) -> str:
    cwd_line = f"\n  cwd: {cwd}" if cwd is not None else ""
    env_block = f"\n  env:\n    DEMO_TOKEN: {env_source}" if env_source is not None else ""
    duplicate = (
        """
  - tool_id: echo
    mcp_name: demo.echo.duplicate
    right: read
    rollback_class: no_rollback_required
    state_mutation: false
    information_flow: true
"""
        if duplicate_tool
        else ""
    )
    return f"""
schema_version: 1
server_id: {server_id}
transport: stdio
stdio:
  command: {command}
  args: ["-m", "demo_server"]{env_block}{cwd_line}
tools:
  - tool_id: echo
    mcp_name: {mcp_name}
    right: read
    rollback_class: no_rollback_required
    state_mutation: {str(state_mutation).lower()}
    information_flow: true
    input_schema:
      type: object
      properties:
        text:
          type: string
      additionalProperties: false
{duplicate}
timeout_s: 5
max_request_bytes: 65536
max_response_bytes: 1048576
""".strip()


def _http_manifest(
    server_id: str,
    url: str,
    *,
    literal_header: bool = False,
    header_env: str = "AGENT_LIBOS_MCP_TEST_TOKEN",
) -> str:
    header = "literal-secret" if literal_header else f"{{env: {header_env}, prefix: 'Bearer '}}"
    return f"""
schema_version: 1
server_id: {server_id}
transport: streamable_http
http:
  url: {url}
  headers:
    Authorization: {header}
tools:
  - tool_id: echo
    mcp_name: demo.echo
    right: read
    rollback_class: no_rollback_required
    state_mutation: false
    information_flow: true
timeout_s: 5
max_request_bytes: 65536
max_response_bytes: 1048576
""".strip()


def _manifest_without_server_id() -> str:
    return """
schema_version: 1
transport: stdio
stdio:
  command: python3
  args: ["-m", "demo_server"]
tools:
  - tool_id: echo
    mcp_name: demo.echo
    right: read
    rollback_class: no_rollback_required
    state_mutation: false
    information_flow: true
""".strip()


class _RecordingMcpProvider:
    def __init__(self, *, live_schema: dict[str, Any] | None = None) -> None:
        self.live_schema = live_schema or {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "additionalProperties": False,
        }
        self.list_calls: list[str] = []
        self.call_args: list[tuple[str, str, dict[str, Any]]] = []

    def list_tools(self, server: Any, **_kwargs: Any) -> McpToolListResult:
        self.list_calls.append(server.server_id)
        return McpToolListResult(
            server_id=server.server_id,
            tools=[McpProviderTool(name="demo.echo", description="Echo", input_schema=self.live_schema)],
            response_bytes=128,
            duration_s=0.01,
        )

    def call_tool(self, server: Any, tool: Any, arguments: dict[str, Any], **_kwargs: Any) -> McpProviderCallResult:
        self.call_args.append((server.server_id, tool.tool_id, dict(arguments)))
        return McpProviderCallResult(
            structured_content={"echo": dict(arguments)},
            content=[{"type": "text", "text": "ok"}],
            response_bytes=64,
            duration_s=0.02,
        )

    def classify_external_effect(
        self,
        operation: str,
        context: dict[str, Any],
        result: Any,
    ) -> ExternalEffectClassification:
        if operation == "list_tools":
            return ExternalEffectClassification(
                rollback_class=ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED,
                rollback_status=ExternalEffectRollbackStatus.NOT_REQUIRED,
                state_mutation=False,
                information_flow=True,
                metadata={"operation": operation, "server_id": context["server_id"]},
            )
        assert operation == "call_tool"
        return ExternalEffectClassification(
            rollback_class=ExternalEffectRollbackClass(str(context["rollback_class"])),
            rollback_status=ExternalEffectRollbackStatus.NOT_REQUIRED,
            state_mutation=bool(context["state_mutation"]),
            information_flow=bool(context["information_flow"]),
        )


class _CallOnlyClassifierMcpProvider(_RecordingMcpProvider):
    def classify_external_effect(
        self,
        operation: str,
        context: dict[str, Any],
        result: Any,
    ) -> ExternalEffectClassification:
        if operation != "call_tool":
            raise ValueError(f"unsupported operation: {operation}")
        return super().classify_external_effect(operation, context, result)


class _FailingListMcpProvider(_RecordingMcpProvider):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def list_tools(self, server: Any, **_kwargs: Any) -> McpToolListResult:
        self.list_calls.append(server.server_id)
        raise RuntimeError(self.message)

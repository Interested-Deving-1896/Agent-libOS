from __future__ import annotations

import asyncio
import hashlib
import socket
from typing import Any

import pytest

from agent_libos import Runtime
from agent_libos.capability.rules import AUTHORITY_RULES_KEY
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
from agent_libos.models.exceptions import CapabilityDenied, HumanApprovalRequired, NotFound, ValidationError
from agent_libos.substrate import ProviderEffectNotStarted
from agent_libos.runtime.syscalls import LibOSSyscallSession
from agent_libos.substrate.local import _allowed_mcp_connect_addresses
from agent_libos.utils.serde import dumps


def _grant_stdio_spawn(
    runtime: Runtime,
    pid: str,
    *,
    command: str = "python3",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> None:
    selected_args = ["-m", "demo_server"] if args is None else list(args)
    runtime.capability.grant(pid, "process:spawn", [CapabilityRight.WRITE], issued_by="test")
    runtime.capability.grant(
        pid,
        runtime.mcp.stdio_resource_for_argv(command, selected_args, env=env, cwd=cwd),
        [CapabilityRight.EXECUTE],
        issued_by="test",
    )


class TestMcpPrimitive:
    @pytest.mark.parametrize('operation', ['inspect', 'list_tools', 'unregister', 'register', 'replace'])
    @pytest.mark.parametrize('server_id', ['secret-existing', 'secret-missing'])
    def test_registry_item_authority_precedes_server_metadata_load(
        self,
        monkeypatch: pytest.MonkeyPatch,
        operation: str,
        server_id: str,
    ) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='mcp registry oracle')

            def fail_if_loaded(_server_id: str) -> Any:
                raise AssertionError('server metadata must not load before authority')

            monkeypatch.setattr(runtime.store, 'get_mcp_server', fail_if_loaded)
            with pytest.raises(CapabilityDenied):
                if operation == 'inspect':
                    runtime.mcp.inspect_server(server_id, actor=pid)
                elif operation == 'list_tools':
                    runtime.mcp.list_tools(server_id, actor=pid, refresh=True)
                elif operation == 'unregister':
                    runtime.mcp.unregister_server(server_id, actor=pid)
                else:
                    runtime.mcp.register_server_from_yaml_text(
                        _stdio_manifest(server_id),
                        actor=pid,
                        replace=operation == 'replace',
                    )
        finally:
            runtime.close()

    def test_registry_register_audit_failure_rolls_back_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime = Runtime.open('local')
        original_record = runtime.audit.record

        def fail_register_audit(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get('action') == 'mcp.server.register':
                raise RuntimeError('injected mcp register audit failure')
            return original_record(*args, **kwargs)

        monkeypatch.setattr(runtime.audit, 'record', fail_register_audit)
        try:
            with pytest.raises(RuntimeError, match='register audit failure'):
                runtime.mcp.register_server_from_yaml_text(
                    _stdio_manifest('register-rollback'),
                    actor='cli',
                    require_capability=False,
                )
            assert runtime.store.get_mcp_server('register-rollback') is None
        finally:
            runtime.close()

    def test_registry_unregister_audit_failure_rolls_back_server_and_tool_caps(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.mcp.register_server_from_yaml_text(
                _stdio_manifest('unregister-rollback'),
                actor='cli',
                require_capability=False,
            )
            pid = runtime.process.spawn(image='base-agent:v0', goal='mcp unregister rollback')
            cap = runtime.capability.grant(
                pid,
                'mcp:unregister-rollback:echo',
                [CapabilityRight.READ],
                issued_by='test',
            )
            original_record = runtime.audit.record

            def fail_unregister_audit(*args: Any, **kwargs: Any) -> Any:
                if kwargs.get('action') == 'mcp.server.unregister':
                    raise RuntimeError('injected mcp unregister audit failure')
                return original_record(*args, **kwargs)

            monkeypatch.setattr(runtime.audit, 'record', fail_unregister_audit)
            with pytest.raises(RuntimeError, match='unregister audit failure'):
                runtime.mcp.unregister_server(
                    'unregister-rollback',
                    actor='cli',
                    require_capability=False,
                )

            assert runtime.store.get_mcp_server('unregister-rollback') is not None
            persisted = runtime.store.get_capability(cap.cap_id)
            assert persisted is not None and persisted.active and not persisted.revoked
        finally:
            runtime.close()

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

    def test_stdio_register_requires_process_spawn_in_actor_mode(self) -> None:
        runtime = Runtime.open("local")
        try:
            actor = runtime.process.spawn(image="base-agent:v0", goal="mcp stdio register")
            runtime.capability.grant(actor, "mcp_server:stdio-register", [CapabilityRight.WRITE], issued_by="test")

            with pytest.raises(CapabilityDenied, match="process:spawn"):
                runtime.mcp.register_server_from_yaml_text(
                    _stdio_manifest("stdio-register"),
                    actor=actor,
                    require_capability=True,
                )

            runtime.capability.grant(actor, "process:spawn", [CapabilityRight.WRITE], issued_by="test")
            with pytest.raises(CapabilityDenied, match="mcp_stdio"):
                runtime.mcp.register_server_from_yaml_text(
                    _stdio_manifest("stdio-register"),
                    actor=actor,
                    require_capability=True,
                )

            _grant_stdio_spawn(runtime, actor)
            registered = runtime.mcp.register_server_from_yaml_text(
                _stdio_manifest("stdio-register"),
                actor=actor,
                require_capability=True,
            )

            assert registered["server_id"] == "stdio-register"
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
            _grant_stdio_spawn(runtime, pid)
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

    @pytest.mark.parametrize('sink', ['event', 'audit'])
    def test_list_tools_refresh_post_provider_sink_failure_leaves_pending_effect_intent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sink: str,
    ) -> None:
        runtime = Runtime.open('local')
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        resource = 'mcp_server:pending-list'
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal=f'mcp list {sink} sink failure')
            runtime.mcp.register_server_from_yaml_text(
                _stdio_manifest('pending-list'),
                actor='cli',
                require_capability=False,
            )
            if sink == 'event':
                original_emit = runtime.events.emit

                def fail_result_event(event_type: Any, *args: Any, **kwargs: Any) -> Any:
                    if kwargs.get('target') == resource:
                        raise RuntimeError('injected mcp list event failure')
                    return original_emit(event_type, *args, **kwargs)

                monkeypatch.setattr(runtime.events, 'emit', fail_result_event)
            else:
                original_record = runtime.audit.record

                def fail_result_audit(*args: Any, **kwargs: Any) -> Any:
                    if kwargs.get('action') == 'primitive.mcp.list_tools':
                        raise RuntimeError('injected mcp list audit failure')
                    return original_record(*args, **kwargs)

                monkeypatch.setattr(runtime.audit, 'record', fail_result_audit)

            with pytest.raises(RuntimeError, match=f'injected mcp list {sink} failure'):
                runtime.mcp.list_tools(
                    'pending-list',
                    actor=pid,
                    require_capability=False,
                    refresh=True,
                )

            assert provider.list_calls == ['pending-list']
            effects = runtime.store.list_external_effects(pid=pid)
            assert len(effects) == 1
            assert effects[0].provider == 'mcp'
            assert effects[0].operation == 'list_tools'
            assert effects[0].effect_state == 'pending'
        finally:
            runtime.close()

    @pytest.mark.parametrize('sink', ['event', 'audit'])
    def test_call_tool_post_provider_sink_failure_leaves_pending_effect_intent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sink: str,
    ) -> None:
        runtime = Runtime.open('local')
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        resource = 'mcp:pending-call:echo'
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal=f'mcp call {sink} sink failure')
            runtime.mcp.register_server_from_yaml_text(
                _stdio_manifest('pending-call'),
                actor='cli',
                require_capability=False,
            )
            runtime.capability.grant(pid, resource, [CapabilityRight.READ], issued_by='test')
            _grant_stdio_spawn(runtime, pid)
            if sink == 'event':
                original_emit = runtime.events.emit

                def fail_result_event(event_type: Any, *args: Any, **kwargs: Any) -> Any:
                    if kwargs.get('target') == resource:
                        raise RuntimeError('injected mcp call event failure')
                    return original_emit(event_type, *args, **kwargs)

                monkeypatch.setattr(runtime.events, 'emit', fail_result_event)
            else:
                original_record = runtime.audit.record

                def fail_result_audit(*args: Any, **kwargs: Any) -> Any:
                    if kwargs.get('action') == 'primitive.mcp.call':
                        raise RuntimeError('injected mcp call audit failure')
                    return original_record(*args, **kwargs)

                monkeypatch.setattr(runtime.audit, 'record', fail_result_audit)

            with pytest.raises(RuntimeError, match=f'injected mcp call {sink} failure'):
                runtime.mcp.call_tool(pid, 'pending-call', 'echo', {'text': 'hello'})

            assert provider.list_calls == ['pending-call']
            assert provider.call_args == [('pending-call', 'echo', {'text': 'hello'})]
            effects = runtime.store.list_external_effects(pid=pid)
            assert len(effects) == 1
            assert effects[0].provider == 'mcp'
            assert effects[0].operation == 'call_tool'
            assert effects[0].effect_state == 'pending'
        finally:
            runtime.close()

    @pytest.mark.parametrize('entry_point', ['refresh', 'call_validation'])
    def test_list_tools_provider_not_started_abandons_effect_intent(self, entry_point: str) -> None:
        runtime = Runtime.open('local')
        provider = _NotStartedListMcpProvider()
        runtime.mcp.provider = provider
        server_id = f'not-started-{entry_point}'
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal=f'mcp {entry_point} not started')
            runtime.mcp.register_server_from_yaml_text(
                _stdio_manifest(server_id),
                actor='cli',
                require_capability=False,
            )
            main_cap = None
            if entry_point == 'call_validation':
                main_cap = runtime.capability.grant_once(
                    pid,
                    f'mcp:{server_id}:echo',
                    [CapabilityRight.READ],
                    issued_by='test',
                )
                _grant_stdio_spawn(runtime, pid)

            with pytest.raises(ProviderEffectNotStarted, match='before list transport'):
                if entry_point == 'refresh':
                    runtime.mcp.list_tools(
                        server_id,
                        actor=pid,
                        require_capability=False,
                        refresh=True,
                    )
                else:
                    runtime.mcp.call_tool(pid, server_id, 'echo', {'text': 'hello'})

            assert provider.list_calls == [server_id]
            assert provider.call_args == []
            assert runtime.store.list_external_effects(pid=pid) == []
            if main_cap is not None:
                persisted = runtime.store.get_capability(main_cap.cap_id)
                assert persisted is not None and persisted.uses_remaining == 1
        finally:
            runtime.close()

    def test_call_tool_not_started_after_live_validation_finalizes_unknown_information_flow(self) -> None:
        runtime = Runtime.open('local')
        provider = _NotStartedCallMcpProvider()
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='mcp call not started after validation')
            runtime.mcp.register_server_from_yaml_text(
                _stdio_manifest('call-not-started'),
                actor='cli',
                require_capability=False,
            )
            runtime.capability.grant(
                pid,
                'mcp:call-not-started:echo',
                [CapabilityRight.READ],
                issued_by='test',
            )
            _grant_stdio_spawn(runtime, pid)

            result = runtime.mcp.call_tool(pid, 'call-not-started', 'echo', {'text': 'hello'})

            assert not result.ok
            assert result.status.value == 'transport_error'
            assert provider.list_calls == ['call-not-started']
            assert provider.call_args == [('call-not-started', 'echo', {'text': 'hello'})]
            effects = runtime.store.list_external_effects(pid=pid)
            assert len(effects) == 1
            effect = effects[0]
            assert effect.operation == 'call_tool'
            assert effect.effect_state == 'finalized'
            assert effect.rollback_class == ExternalEffectRollbackClass.UNKNOWN
            assert effect.rollback_status == ExternalEffectRollbackStatus.UNKNOWN
            assert not effect.state_mutation
            assert effect.information_flow
            assert effect.provider_metadata['outcome'] == 'call_tool_not_started_after_live_validation'
        finally:
            runtime.close()

    def test_stdio_live_validation_not_started_restores_all_finite_authority(self) -> None:
        runtime = Runtime.open('local')
        provider = _NotStartedListMcpProvider()
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='mcp composite authority restore')
            runtime.mcp.register_server_from_yaml_text(
                _stdio_manifest('composite-restore'),
                actor='cli',
                require_capability=False,
            )
            main = runtime.capability.grant_once(
                pid,
                'mcp:composite-restore:echo',
                [CapabilityRight.READ],
                issued_by='test',
            )
            spawn = runtime.capability.grant_once(
                pid,
                'process:spawn',
                [CapabilityRight.WRITE],
                issued_by='test',
            )
            stdio = runtime.capability.grant_once(
                pid,
                runtime.mcp.stdio_resource_for_argv('python3', ['-m', 'demo_server']),
                [CapabilityRight.EXECUTE],
                issued_by='test',
            )

            with pytest.raises(ProviderEffectNotStarted, match='before list transport'):
                runtime.mcp.call_tool(pid, 'composite-restore', 'echo', {'text': 'hello'})

            for cap in (main, spawn, stdio):
                persisted = runtime.store.get_capability(cap.cap_id)
                assert persisted is not None and persisted.uses_remaining == 1
            assert runtime.store.list_external_effects(pid=pid) == []
        finally:
            runtime.close()

    def test_stdio_success_commits_all_finite_authority(self) -> None:
        runtime = Runtime.open('local')
        runtime.mcp.provider = _RecordingMcpProvider()
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='mcp composite authority commit')
            runtime.mcp.register_server_from_yaml_text(
                _stdio_manifest('composite-commit'),
                actor='cli',
                require_capability=False,
            )
            caps = [
                runtime.capability.grant_once(
                    pid,
                    'mcp:composite-commit:echo',
                    [CapabilityRight.READ],
                    issued_by='test',
                ),
                runtime.capability.grant_once(
                    pid,
                    'process:spawn',
                    [CapabilityRight.WRITE],
                    issued_by='test',
                ),
                runtime.capability.grant_once(
                    pid,
                    runtime.mcp.stdio_resource_for_argv('python3', ['-m', 'demo_server']),
                    [CapabilityRight.EXECUTE],
                    issued_by='test',
                ),
            ]

            assert runtime.mcp.call_tool(pid, 'composite-commit', 'echo', {'text': 'hello'}).ok

            for cap in caps:
                persisted = runtime.store.get_capability(cap.cap_id)
                assert persisted is not None and persisted.uses_remaining == 0
        finally:
            runtime.close()

    def test_list_refresh_deduplicates_one_capability_selected_for_read_and_execute(self) -> None:
        runtime = Runtime.open('local')
        runtime.mcp.provider = _NotStartedListMcpProvider()
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='mcp refresh authority dedup')
            runtime.mcp.register_server_from_yaml_text(
                _stdio_manifest('refresh-dedup'),
                actor='cli',
                require_capability=False,
            )
            cap = runtime.capability.grant_once(
                pid,
                'mcp_server:refresh-dedup',
                [CapabilityRight.READ, CapabilityRight.EXECUTE],
                issued_by='test',
            )
            _grant_stdio_spawn(runtime, pid)

            with pytest.raises(ProviderEffectNotStarted, match='before list transport'):
                runtime.mcp.list_tools('refresh-dedup', actor=pid, refresh=True)
            restored = runtime.store.get_capability(cap.cap_id)
            assert restored is not None and restored.uses_remaining == 1

            runtime.mcp.provider = _RecordingMcpProvider()
            assert runtime.mcp.list_tools('refresh-dedup', actor=pid, refresh=True)['refreshed']
            committed = runtime.store.get_capability(cap.cap_id)
            assert committed is not None and committed.uses_remaining == 0
        finally:
            runtime.close()

    def test_http_resolution_certified_not_started_restores_all_authority(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open('local')
        runtime.mcp.provider = _RecordingMcpProvider()
        monkeypatch.setenv('AGENT_LIBOS_MCP_TEST_TOKEN', 'token')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='mcp resolution not started')
            runtime.mcp.register_server_from_yaml_text(
                _http_manifest('resolution-not-started', 'https://mcp.example.test/tools'),
                actor='cli',
                require_capability=False,
            )
            cap = runtime.capability.grant_once(
                pid,
                'mcp:resolution-not-started:echo',
                [CapabilityRight.READ],
                issued_by='test',
            )
            monkeypatch.setattr(
                runtime.mcp,
                '_validate_runtime_resolution',
                lambda _spec: (_ for _ in ()).throw(ProviderEffectNotStarted('resolution did not start')),
            )

            with pytest.raises(ProviderEffectNotStarted, match='resolution did not start'):
                runtime.mcp.call_tool(pid, 'resolution-not-started', 'echo', {'text': 'hello'})

            persisted = runtime.store.get_capability(cap.cap_id)
            assert persisted is not None and persisted.uses_remaining == 1
            assert runtime.store.list_external_effects(pid=pid) == []
        finally:
            runtime.close()

    def test_http_live_validation_not_started_after_dns_keeps_information_flow(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open('local')
        provider = _NotStartedListMcpProvider()
        runtime.mcp.provider = provider
        monkeypatch.setenv('AGENT_LIBOS_MCP_TEST_TOKEN', 'token')
        monkeypatch.setattr(
            'agent_libos.primitives.mcp.socket.getaddrinfo',
            lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))],
        )
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='mcp post-dns not started')
            runtime.mcp.register_server_from_yaml_text(
                _http_manifest('post-dns-not-started', 'https://mcp.example.test/tools'),
                actor='cli',
                require_capability=False,
            )
            cap = runtime.capability.grant_once(
                pid,
                'mcp:post-dns-not-started:echo',
                [CapabilityRight.READ],
                issued_by='test',
            )

            with pytest.raises(ProviderEffectNotStarted, match='before list transport'):
                runtime.mcp.call_tool(pid, 'post-dns-not-started', 'echo', {'text': 'hello'})

            persisted = runtime.store.get_capability(cap.cap_id)
            assert persisted is not None and persisted.uses_remaining == 0
            effects = runtime.store.list_external_effects(pid=pid)
            assert len(effects) == 1
            assert effects[0].effect_state == 'finalized'
            assert effects[0].rollback_status == ExternalEffectRollbackStatus.UNKNOWN
            assert effects[0].information_flow
            assert effects[0].provider_metadata['phase'] == 'live_validation_not_started_after_dns'
        finally:
            runtime.close()

    def test_local_http_provider_not_started_before_transport_restores_authority(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open('local')
        provider = _NotStartedListMcpProvider()
        runtime.mcp.provider = provider
        monkeypatch.setenv('AGENT_LIBOS_MCP_TEST_TOKEN', 'token')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='local mcp not started')
            runtime.mcp.register_server_from_yaml_text(
                _http_manifest('local-not-started', 'http://localhost:8765/tools'),
                actor='cli',
                require_capability=False,
            )
            cap = runtime.capability.grant_once(
                pid,
                'mcp:local-not-started:echo',
                [CapabilityRight.READ],
                issued_by='test',
            )

            with pytest.raises(ProviderEffectNotStarted, match='before list transport'):
                runtime.mcp.call_tool(pid, 'local-not-started', 'echo', {'text': 'hello'})

            persisted = runtime.store.get_capability(cap.cap_id)
            assert persisted is not None and persisted.uses_remaining == 1
            assert runtime.store.list_external_effects(pid=pid) == []
        finally:
            runtime.close()

    def test_stdio_call_requires_process_spawn_before_consuming_tool_capability(self) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp stdio spawn authority")
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("stdio-spawn"), actor="cli", require_capability=False)
            cap = runtime.capability.grant_once(pid, "mcp:stdio-spawn:echo", [CapabilityRight.READ], issued_by="test")

            with pytest.raises(CapabilityDenied, match="process:spawn"):
                runtime.mcp.call_tool(pid, "stdio-spawn", "echo", {"text": "hello"})

            assert provider.list_calls == []
            assert provider.call_args == []
            assert runtime.store.get_capability(cap.cap_id).uses_remaining == 1
        finally:
            runtime.close()

    def test_stdio_call_requires_exact_stdio_spawn_before_consuming_tool_capability(self) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp stdio argv authority")
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("stdio-argv"), actor="cli", require_capability=False)
            cap = runtime.capability.grant_once(pid, "mcp:stdio-argv:echo", [CapabilityRight.READ], issued_by="test")
            runtime.capability.grant(pid, "process:spawn", [CapabilityRight.WRITE], issued_by="test")

            with pytest.raises(CapabilityDenied, match="mcp_stdio"):
                runtime.mcp.call_tool(pid, "stdio-argv", "echo", {"text": "hello"})

            assert provider.list_calls == []
            assert provider.call_args == []
            assert runtime.store.get_capability(cap.cap_id).uses_remaining == 1
        finally:
            runtime.close()

    def test_stdio_call_requires_exact_env_and_cwd_spawn_authority(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        monkeypatch.setenv("AGENT_LIBOS_MCP_ALLOWED_TOKEN", "token")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp stdio env cwd authority")
            runtime.mcp.register_server_from_yaml_text(
                _stdio_manifest(
                    "stdio-env-cwd",
                    env_source="AGENT_LIBOS_MCP_ALLOWED_TOKEN",
                    cwd="server-cwd",
                ),
                actor="cli",
                require_capability=False,
            )
            cap = runtime.capability.grant_once(pid, "mcp:stdio-env-cwd:echo", [CapabilityRight.READ], issued_by="test")
            runtime.capability.grant(pid, "process:spawn", [CapabilityRight.WRITE], issued_by="test")
            runtime.capability.grant(
                pid,
                runtime.mcp.stdio_resource_for_argv("python3", ["-m", "demo_server"]),
                [CapabilityRight.EXECUTE],
                issued_by="test",
            )

            with pytest.raises(CapabilityDenied, match="mcp_stdio"):
                runtime.mcp.call_tool(pid, "stdio-env-cwd", "echo", {"text": "hello"})

            assert provider.list_calls == []
            assert provider.call_args == []
            assert runtime.store.get_capability(cap.cap_id).uses_remaining == 1

            runtime.capability.grant(
                pid,
                runtime.mcp.stdio_resource_for_argv(
                    "python3",
                    ["-m", "demo_server"],
                    env={"DEMO_TOKEN": "AGENT_LIBOS_MCP_ALLOWED_TOKEN"},
                    cwd="server-cwd",
                ),
                [CapabilityRight.EXECUTE],
                issued_by="test",
            )
            result = runtime.mcp.call_tool(pid, "stdio-env-cwd", "echo", {"text": "hello"})

            assert result.ok
            assert provider.call_args == [("stdio-env-cwd", "echo", {"text": "hello"})]
        finally:
            runtime.close()

    def test_stdio_call_requires_process_spawn_before_runtime_env_validation(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        monkeypatch.delenv("AGENT_LIBOS_MCP_REVIEW_MISSING", raising=False)
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp stdio env authority")
            runtime.mcp.register_server_from_yaml_text(
                _stdio_manifest("stdio-env-spawn", env_source="AGENT_LIBOS_MCP_REVIEW_MISSING"),
                actor="cli",
                require_capability=False,
            )
            runtime.capability.grant(pid, "mcp:stdio-env-spawn:echo", [CapabilityRight.READ], issued_by="test")

            with pytest.raises(CapabilityDenied, match="process:spawn"):
                runtime.mcp.call_tool(pid, "stdio-env-spawn", "echo", {"text": "hello"})

            assert provider.list_calls == []
            assert provider.call_args == []
        finally:
            runtime.close()

    def test_stdio_call_requires_process_spawn_before_argument_schema_validation(self) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp stdio schema authority")
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("stdio-schema-spawn"), actor="cli", require_capability=False)
            runtime.capability.grant(pid, "mcp:stdio-schema-spawn:echo", [CapabilityRight.READ], issued_by="test")

            with pytest.raises(CapabilityDenied, match="process:spawn"):
                runtime.mcp.call_tool(pid, "stdio-schema-spawn", "echo", {"unexpected": "secret"})

            assert provider.list_calls == []
            assert provider.call_args == []
        finally:
            runtime.close()

    def test_call_denies_before_loading_server_metadata_without_visibility(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp hidden manifest")

            def fail_if_manifest_loaded(_server_id: str) -> Any:
                raise AssertionError("MCP server manifest should stay hidden before capability gate")

            monkeypatch.setattr(runtime.store, "get_mcp_server", fail_if_manifest_loaded)

            with pytest.raises(CapabilityDenied, match="MCP call authority"):
                runtime.mcp.call_tool(pid, "secret-server", "hidden-tool", {"text": "hello"})
        finally:
            runtime.close()

    def test_call_ask_visibility_prompts_before_loading_server_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp ask hidden manifest")
            runtime.capability.set_permission_policy(
                pid,
                "mcp:secret-server:hidden-tool",
                [CapabilityRight.READ],
                runtime.capability.ASK_EACH_TIME,
                issued_by="test",
            )

            def fail_if_manifest_loaded(_server_id: str) -> Any:
                raise AssertionError("MCP server manifest should stay hidden before human approval")

            monkeypatch.setattr(runtime.store, "get_mcp_server", fail_if_manifest_loaded)

            with pytest.raises(HumanApprovalRequired):
                runtime.mcp.call_tool(pid, "secret-server", "hidden-tool", {"text": "hello"})
        finally:
            runtime.close()

    def test_call_visibility_honors_argument_scoped_authority_rule(self) -> None:
        runtime = Runtime.open("local")
        provider = _RecordingMcpProvider()
        runtime.mcp.provider = provider
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp scoped visibility")
            runtime.mcp.register_server_from_yaml_text(_stdio_manifest("scoped-visibility"), actor="cli", require_capability=False)
            arguments = {"text": "hello"}
            arguments_sha = hashlib.sha256(dumps(arguments).encode("utf-8")).hexdigest()
            runtime.capability.grant(
                pid,
                "mcp:scoped-visibility:echo",
                [CapabilityRight.READ],
                issued_by="test",
                constraints={
                    AUTHORITY_RULES_KEY: [
                        {
                            "rule_id": "mcp.scoped.visibility",
                            "operation": "mcp.call",
                            "effect": "allow",
                            "risk": "low",
                            "conditions": {
                                "server_id": "scoped-visibility",
                                "tool_id": "echo",
                                "arguments_sha256": arguments_sha,
                            },
                        }
                    ]
                },
            )
            _grant_stdio_spawn(runtime, pid)

            result = runtime.mcp.call_tool(pid, "scoped-visibility", "echo", arguments)

            assert result.ok
            assert provider.call_args == [("scoped-visibility", "echo", arguments)]
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
            _grant_stdio_spawn(runtime, pid)

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

    def test_http_dns_private_resolution_consumes_authority_and_records_information_flow(
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
            persisted = runtime.store.get_capability(cap.cap_id)
            assert persisted is not None and persisted.uses_remaining == 0
            effects = runtime.store.list_external_effects(pid=pid)
            assert len(effects) == 1
            assert effects[0].effect_state == 'finalized'
            assert effects[0].rollback_status == ExternalEffectRollbackStatus.UNKNOWN
            assert effects[0].information_flow
            assert effects[0].provider_metadata['phase'] == 'dns_resolution'
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
            _grant_stdio_spawn(runtime, pid)
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
            _grant_stdio_spawn(runtime, pid)

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
            _grant_stdio_spawn(runtime, pid)

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
            _grant_stdio_spawn(runtime, pid)
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
            _grant_stdio_spawn(runtime, actor)
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
            _grant_stdio_spawn(runtime, actor)
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
            _grant_stdio_spawn(runtime, pid)
            checkpoint_id = runtime.checkpoint.create(pid, "before mcp", actor=pid)
            runtime.mcp.call_tool(pid, "ckpt", "echo", {"text": "after"})
            runtime.mcp.unregister_server("ckpt", actor="cli", require_capability=False)

            with pytest.raises(NotFound):
                runtime.mcp.inspect_server("ckpt", require_capability=False)

            restored = runtime.checkpoint.restore("cli", checkpoint_id, require_capability=False)

            assert restored["external_effect_summary"]["by_provider_operation"]["mcp.call_tool"] == 1
            with pytest.raises(NotFound):
                runtime.mcp.inspect_server("ckpt", require_capability=False)
            with pytest.raises(CapabilityDenied, match="MCP call authority"):
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


class _NotStartedListMcpProvider(_RecordingMcpProvider):
    def list_tools(self, server: Any, **_kwargs: Any) -> McpToolListResult:
        self.list_calls.append(server.server_id)
        raise ProviderEffectNotStarted('mcp failed before list transport')


class _NotStartedCallMcpProvider(_RecordingMcpProvider):
    def call_tool(self, server: Any, tool: Any, arguments: dict[str, Any], **_kwargs: Any) -> McpProviderCallResult:
        self.call_args.append((server.server_id, tool.tool_id, dict(arguments)))
        raise ProviderEffectNotStarted('mcp failed before tool transport')


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

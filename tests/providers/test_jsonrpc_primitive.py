from __future__ import annotations
import pytest
import contextlib
import io
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from pytest import MonkeyPatch
from agent_libos import Runtime
from agent_libos.api.cli import main as cli_main
from agent_libos.models import CapabilityRight
from agent_libos.models.exceptions import CapabilityDenied, HumanApprovalRequired, NotFound, ValidationError
from agent_libos.runtime.syscalls import LibOSSyscallSession
from agent_libos.substrate import LocalResourceProviderSubstrate

class TestJsonRpcPrimitive:

    def test_manifest_validation_rejects_unsafe_endpoint_shapes(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.jsonrpc.register_endpoint_from_yaml_text(_manifest('valid', 'https://api.example.test/jsonrpc'), actor='cli', require_capability=False)
            invalid_cases = [_manifest('bad-http', 'http://api.example.test/jsonrpc'), _manifest('bad-userinfo', 'https://user:pass@example.test/jsonrpc'), _manifest('bad-fragment', 'https://api.example.test/jsonrpc#secret'), _manifest('bad:colon', 'https://api.example.test/jsonrpc'), _manifest('missing-class', 'https://api.example.test/jsonrpc', rollback_class=None), _manifest('literal-header', 'https://api.example.test/jsonrpc', literal_header=True), _manifest('bad-bool', 'https://api.example.test/jsonrpc', state_mutation='"false"'), _manifest('dup-method', 'https://api.example.test/jsonrpc', duplicate_method=True)]
            for text in invalid_cases:
                with pytest.raises(ValidationError):
                    runtime.jsonrpc.register_endpoint_from_yaml_text(text, actor='cli', require_capability=False)
        finally:
            runtime.close()

    def test_call_requires_method_capability_and_records_http_effect(self, monkeypatch: MonkeyPatch) -> None:
        with _jsonrpc_server() as server:
            runtime = Runtime.open('local')
            monkeypatch.setenv('AGENT_LIBOS_JSONRPC_TEST_TOKEN', 'secret-token')
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='remote call')
                runtime.jsonrpc.register_endpoint_from_yaml_text(_manifest('demo', server.url), actor='cli', require_capability=False)
                with pytest.raises(CapabilityDenied):
                    runtime.jsonrpc.call(pid, 'demo', 'echo', {'city': 'Beijing'})
                runtime.capability.grant(pid, 'jsonrpc:demo:echo', [CapabilityRight.READ], issued_by='test')
                result = runtime.jsonrpc.call(pid, 'demo', 'echo', {'city': 'Beijing'})
                assert result.ok
                assert result.result['method'] == 'demo.echo'
                assert server.requests[0]['authorization'] == 'Bearer secret-token'
                assert server.requests[0]['body']['jsonrpc'] == '2.0'
                assert server.requests[0]['body']['method'] == 'demo.echo'
                assert 'url' not in (runtime.audit.trace()[-1].decision or {})
                assert 'secret-token' not in json.dumps([record.__dict__ for record in runtime.audit.trace()])
                effect = [item for item in runtime.store.list_external_effects() if item.provider == 'jsonrpc'][0]
                assert effect.rollback_class.value == 'no_rollback_required'
                assert not effect.state_mutation
                assert effect.information_flow
            finally:
                runtime.close()

    def test_jsonrpc_errors_and_http_failures_are_structured_results(self) -> None:
        with _jsonrpc_server() as server:
            runtime = Runtime.open('local')
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='remote errors')
                runtime.jsonrpc.register_endpoint_from_yaml_text(_manifest('errors', server.url, with_header=False), actor='cli', require_capability=False)
                runtime.capability.grant(pid, 'jsonrpc:errors:echo', [CapabilityRight.READ], issued_by='test')
                server.mode = 'jsonrpc_error'
                rpc_error = runtime.jsonrpc.call(pid, 'errors', 'echo', {})
                assert not rpc_error.ok
                assert rpc_error.status.value == 'jsonrpc_error'
                assert rpc_error.error['code'] == -32000
                server.mode = 'http_500'
                http_error = runtime.jsonrpc.call(pid, 'errors', 'echo', {})
                assert not http_error.ok
                assert http_error.status.value == 'http_error'
                assert http_error.http_status == 500
            finally:
                runtime.close()

    def test_missing_header_env_fails_before_http_attempt_and_one_shot_consumption(self, monkeypatch: MonkeyPatch) -> None:
        with _jsonrpc_server() as server:
            runtime = Runtime.open('local')
            monkeypatch.delenv('AGENT_LIBOS_JSONRPC_TEST_TOKEN', raising=False)
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='missing env')
                runtime.jsonrpc.register_endpoint_from_yaml_text(_manifest('needs-token', server.url), actor='cli', require_capability=False)
                cap = runtime.capability.grant_once(pid, 'jsonrpc:needs-token:echo', [CapabilityRight.READ], issued_by='test')
                with pytest.raises(ValidationError):
                    runtime.jsonrpc.call(pid, 'needs-token', 'echo', {})
                assert server.requests == []
                assert runtime.store.get_capability(cap.cap_id).uses_remaining == 1
            finally:
                runtime.close()

    def test_human_jsonrpc_approval_is_bound_to_params_hash(self) -> None:
        with _jsonrpc_server() as server:
            runtime = Runtime.open('local')
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='jsonrpc approval')
                runtime.jsonrpc.register_endpoint_from_yaml_text(_manifest('approval', server.url, with_header=False), actor='cli', require_capability=False)
                runtime.capability.set_permission_policy(
                    pid,
                    'jsonrpc:approval:echo',
                    [CapabilityRight.READ],
                    runtime.capability.ASK_EACH_TIME,
                    issued_by='test',
                )
                with pytest.raises(HumanApprovalRequired):
                    runtime.jsonrpc.call(pid, 'approval', 'echo', {'x': 1})
                runtime.human.drain_terminal_queue(auto_approve=True)
                with pytest.raises(HumanApprovalRequired):
                    runtime.jsonrpc.call(pid, 'approval', 'echo', {'x': 2})
                assert server.requests == []
                result = runtime.jsonrpc.call(pid, 'approval', 'echo', {'x': 1})
                assert result.ok
                assert len(server.requests) == 1
            finally:
                runtime.close()

    def test_effectful_provider_without_classifier_fails_closed(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='missing classifier')
            runtime.jsonrpc.register_endpoint_from_yaml_text(_manifest('no-classifier', 'https://api.example.test/jsonrpc', with_header=False), actor='cli', require_capability=False)
            runtime.capability.grant(pid, 'jsonrpc:no-classifier:echo', [CapabilityRight.READ], issued_by='test')
            runtime.jsonrpc.provider = _ProviderWithoutClassifier()
            with pytest.raises(ValidationError):
                runtime.jsonrpc.call(pid, 'no-classifier', 'echo', {})
        finally:
            runtime.close()

    def test_syscall_bypasses_tool_table_but_not_capabilities(self) -> None:
        with _jsonrpc_server() as server:
            runtime = Runtime.open('local')
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='syscall remote')
                process = runtime.process.get(pid)
                process.tool_table.pop('call_jsonrpc_method', None)
                runtime.store.update_process(process)
                runtime.jsonrpc.register_endpoint_from_yaml_text(_manifest('sys', server.url, with_header=False), actor='cli', require_capability=False)
                session = LibOSSyscallSession(runtime, pid)
                with pytest.raises(CapabilityDenied):
                    self._run(session.handle('jsonrpc.call', {'endpoint_id': 'sys', 'method_id': 'echo', 'params': {}}))
                runtime.capability.grant(pid, 'jsonrpc:sys:echo', [CapabilityRight.READ], issued_by='test')
                result = self._run(session.handle('jsonrpc.call', {'endpoint_id': 'sys', 'method_id': 'echo', 'params': {}}))
                assert result['ok']
                assert 'call_jsonrpc_method' not in runtime.process.get(pid).tool_table
            finally:
                runtime.close()

    def test_checkpoint_restores_referenced_endpoint_and_reports_remote_effect(self) -> None:
        with _jsonrpc_server() as server:
            runtime = Runtime.open('local')
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='checkpoint remote')
                runtime.jsonrpc.register_endpoint_from_yaml_text(_manifest('ckpt', server.url, with_header=False), actor='cli', require_capability=False)
                runtime.capability.grant(pid, 'jsonrpc:ckpt:echo', [CapabilityRight.READ], issued_by='test')
                checkpoint_id = runtime.checkpoint.create(pid, 'before remote', actor=pid)
                runtime.jsonrpc.call(pid, 'ckpt', 'echo', {'x': 1})
                runtime.jsonrpc.unregister_endpoint('ckpt', actor='cli', require_capability=False)
                with pytest.raises(NotFound):
                    runtime.jsonrpc.inspect_endpoint('ckpt', require_capability=False)
                restored = runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)
                assert restored['external_effect_summary']['by_provider_operation']['jsonrpc.call'] == 1
                assert runtime.jsonrpc.inspect_endpoint('ckpt', require_capability=False)['endpoint_id'] == 'ckpt'
            finally:
                runtime.close()

    def test_cli_register_list_inspect_and_call(self, monkeypatch: MonkeyPatch) -> None:
        with _jsonrpc_server() as server, tempfile.TemporaryDirectory() as temp_dir:
            monkeypatch.setenv('AGENT_LIBOS_JSONRPC_TEST_TOKEN', 'cli-token')
            db = str(Path(temp_dir) / 'runtime.sqlite')
            manifest = Path(temp_dir) / 'endpoint.yaml'
            manifest.write_text(_manifest('cli-demo', server.url), encoding='utf-8')
            spawned = _run_cli_json(['--db', db, 'spawn', '--goal', 'cli jsonrpc'])
            registered = _run_cli_json(['--db', db, 'jsonrpc', 'register', str(manifest)])
            listed = _run_cli_json(['--db', db, 'jsonrpc', 'list'])
            inspected = _run_cli_json(['--db', db, 'jsonrpc', 'inspect', 'cli-demo'])
            _run_cli_json(['--db', db, 'capabilities', 'grant', spawned['pid'], 'jsonrpc:cli-demo:echo', '--rights', 'read'])
            called = _run_cli_json(['--db', db, 'jsonrpc', 'call', spawned['pid'], 'cli-demo', 'echo', '--params-json', '{"q": "ok"}'])
            assert registered['endpoint_id'] == 'cli-demo'
            assert listed[0]['endpoint_id'] == 'cli-demo'
            assert listed[0]['url'] is None
            assert inspected['url'] == server.url
            assert called['ok']

    def _run(self, awaitable: Any) -> Any:
        import asyncio
        return asyncio.run(awaitable)

class _JsonRpcServer:

    def __init__(self) -> None:
        self.mode = 'ok'
        self.requests: list[dict[str, Any]] = []
        self.httpd: HTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.url = ''

    def __enter__(self) -> '_JsonRpcServer':
        owner = self

        class Handler(BaseHTTPRequestHandler):

            def do_POST(self) -> None:
                length = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(length)
                body = json.loads(raw.decode('utf-8'))
                owner.requests.append({'path': self.path, 'authorization': self.headers.get('Authorization'), 'body': body})
                if owner.mode == 'http_500':
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b'server failure')
                    return
                response: dict[str, Any]
                if owner.mode == 'jsonrpc_error':
                    response = {'jsonrpc': '2.0', 'id': body.get('id'), 'error': {'code': -32000, 'message': 'remote failure'}}
                else:
                    response = {'jsonrpc': '2.0', 'id': body.get('id'), 'result': {'method': body.get('method'), 'params': body.get('params')}}
                payload = json.dumps(response).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args: Any) -> None:
                return
        self.httpd = HTTPServer(('127.0.0.1', 0), Handler)
        port = int(self.httpd.server_address[1])
        self.url = f'http://127.0.0.1:{port}/rpc'
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

def _jsonrpc_server() -> _JsonRpcServer:
    return _JsonRpcServer()

class _ProviderWithoutClassifier:

    def call(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError('provider call should not run without an external-effect classifier')

def _manifest(endpoint_id: str, url: str, *, rollback_class: str | None='no_rollback_required', state_mutation: bool | str=False, with_header: bool=True, literal_header: bool=False, duplicate_method: bool=False) -> str:
    header = ''
    if literal_header:
        header = '\nheaders:\n  Authorization: "Bearer literal-secret"\n'
    elif with_header:
        header = '\nheaders:\n  Authorization:\n    env: AGENT_LIBOS_JSONRPC_TEST_TOKEN\n    prefix: "Bearer "\n'
    rollback = f'    rollback_class: {rollback_class}\n' if rollback_class is not None else ''
    second = '\n  - method_id: echo\n    rpc_method: demo.echo2\n    right: read\n    rollback_class: no_rollback_required\n    state_mutation: false\n    information_flow: true\n' if duplicate_method else ''
    state = 'true' if state_mutation is True else 'false' if state_mutation is False else state_mutation
    return f'\nschema_version: 1\nendpoint_id: {endpoint_id}\nurl: {url}\n{header}methods:\n  - method_id: echo\n    rpc_method: demo.echo\n    right: read\n{rollback}    state_mutation: {state}\n    information_flow: true\n{second}timeout_s: 5\nmax_request_bytes: 65536\nmax_response_bytes: 1048576\n'.lstrip()

def _run_cli_json(argv: list[str]) -> Any:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cli_main(argv)
    return json.loads(buffer.getvalue())

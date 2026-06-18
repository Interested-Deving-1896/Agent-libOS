from __future__ import annotations
import pytest
import http.client
import json
import threading
import time
import urllib.request
from typing import Any
from agent_libos.api.gui.server import create_gui_http_server

class TestGuiServer:

    def setup_method(self) -> None:
        self.server = create_gui_http_server(db='local', port=0, token='test-token', auto_run=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def teardown_method(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.service.shutdown()
        self.server.server_close()

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        token: str = 'test-token',
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = {'Authorization': f'Bearer {token}'}
        headers.update(extra_headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        decoded = json.loads(data.decode('utf-8')) if data else None
        return (response.status, decoded)

    def request_raw(
        self,
        method: str,
        path: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = {'Authorization': 'Bearer test-token'}
        headers.update(extra_headers or {})
        conn.request(method, path, headers=headers)
        response = conn.getresponse()
        data = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        conn.close()
        return response.status, response_headers, data

    def test_auth_health_snapshot_and_process_flow(self) -> None:
        status, _body = self.request('GET', '/api/health', token='wrong')
        assert status == 401
        status, health = self.request('GET', '/api/health')
        assert status == 200
        assert health['ok']
        assert not health['scheduler']['auto_run']
        assert health['scheduler']['default_max_quanta'] is None
        status, spawned = self.request('POST', '/api/processes', {'goal': 'inspect README', 'auto_run': False})
        assert status == 200
        pid = spawned['pid']
        status, message = self.request('POST', f'/api/processes/{pid}/message', {'body': 'hello', 'auto_run': False})
        assert status == 200
        assert message['message']['body'] == 'hello'
        status, interrupt = self.request('POST', f'/api/processes/{pid}/interrupt', {'body': 'stop', 'auto_run': False})
        assert status == 200
        assert interrupt['message']['kind'] == 'interrupt'
        status, snapshot = self.request('GET', '/api/snapshot')
        assert status == 200
        assert len(snapshot['processes']) == 1
        assert snapshot['processes'][0]['unread_message_count'] >= 2
        assert 'tools' in snapshot
        assert 'images' in snapshot
        assert any((image['image_id'] == 'base-agent:v0' for image in snapshot['images']))

    def test_cors_is_limited_to_local_gui_origins(self) -> None:
        status, headers, _body = self.request_raw(
            'OPTIONS',
            '/api/health',
            extra_headers={'Origin': 'http://127.0.0.1:5173'},
        )
        assert status == 204
        assert headers['access-control-allow-origin'] == 'http://127.0.0.1:5173'

        status, headers, _body = self.request_raw(
            'OPTIONS',
            '/api/health',
            extra_headers={'Origin': 'https://example.test'},
        )
        assert status == 204
        assert 'access-control-allow-origin' not in headers

    def test_sse_replays_snapshot_event(self) -> None:
        request = urllib.request.Request(f'http://{self.host}:{self.port}/api/events/stream?cursor=0', headers={'Authorization': 'Bearer test-token'})
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200
            frame_lines: list[str] = []
            while len(frame_lines) < 3:
                line = response.readline().decode('utf-8').strip()
                if line:
                    frame_lines.append(line)
            assert frame_lines[0].startswith('id: ')
            assert frame_lines[1] == 'event: snapshot'
            assert frame_lines[2].startswith('data: ')

    def test_high_risk_exec_requires_confirmation(self) -> None:
        _status, spawned = self.request('POST', '/api/processes', {'goal': 'goal', 'auto_run': False})
        pid = spawned['pid']
        status, denied = self.request('POST', f'/api/processes/{pid}/exec', {'image': 'base-agent:v0', 'goal': 'new'})
        assert status == 409
        assert denied['error']['confirmation_required']
        status, string_confirmed = self.request('POST', f'/api/processes/{pid}/exec', {'image': 'base-agent:v0', 'goal': 'new', 'confirmed': 'true'})
        assert status == 409
        assert string_confirmed['error']['confirmation_required']
        status, allowed = self.request('POST', f'/api/processes/{pid}/exec', {'image': 'base-agent:v0', 'goal': 'new', 'confirmed': True, 'auto_run': False})
        assert status == 200
        assert allowed['process']['image_id'] == 'base-agent:v0'

    def test_high_risk_image_commit_requires_confirmation(self) -> None:
        _status, spawned = self.request('POST', '/api/processes', {'goal': 'commit source', 'auto_run': False})
        pid = spawned['pid']
        status, created = self.request('POST', '/api/checkpoints/create', {'pid': pid, 'reason': 'commit'})
        assert status == 200
        status, denied = self.request('POST', '/api/images/commit', {'checkpoint_id': created['checkpoint_id'], 'image_id': 'gui-committed:v0', 'name': 'gui-committed'})
        assert status == 409
        assert denied['error']['confirmation_required']
        status, forbidden = self.request('POST', '/api/images/commit', {'checkpoint_id': created['checkpoint_id'], 'image_id': 'gui-committed:v0', 'name': 'gui-committed', 'actor': pid, 'confirmed': True})
        assert status == 403
        assert 'lacks write' in forbidden['error']['message']
        status, committed = self.request('POST', '/api/images/commit', {'checkpoint_id': created['checkpoint_id'], 'image_id': 'gui-committed:v0', 'name': 'gui-committed', 'confirmed': True})
        assert status == 200
        assert committed['image_id'] == 'gui-committed:v0'
        status, inspected = self.request('GET', '/api/images/gui-committed:v0')
        assert status == 200
        assert inspected['image']['boot']['kind'] == 'checkpoint_commit'

    def test_checkpoint_actor_mode_enforces_restore_capability(self) -> None:
        _status, owner = self.request('POST', '/api/processes', {'goal': 'checkpoint owner', 'auto_run': False})
        _status, other = self.request('POST', '/api/processes', {'goal': 'unprivileged actor', 'auto_run': False})
        status, created = self.request('POST', '/api/checkpoints/create', {'pid': owner['pid'], 'reason': 'admin checkpoint'})
        assert status == 200
        status, body = self.request(
            'POST',
            f"/api/checkpoints/{created['checkpoint_id']}/restore",
            {'actor': other['pid'], 'confirmed': True},
        )
        assert status == 403
        assert 'checkpoint' in body['error']['message']

    def test_image_register_accepts_manifest_text_and_rejects_host_file_path(self) -> None:
        manifest = '\nimage:\n  image_id: gui-yaml-agent:v0\n  name: gui-yaml-agent\n  version: v0\n  system_prompt: Registered from GUI manifest text.\n'
        status, denied = self.request('POST', '/api/images/register', {'manifest_text': manifest, 'source': 'gui-yaml-agent.yaml'})
        assert status == 409
        assert denied['error']['confirmation_required']
        status, string_confirmed = self.request('POST', '/api/images/register', {'manifest_text': manifest, 'source': 'gui-yaml-agent.yaml', 'confirmed': 'true'})
        assert status == 409
        assert string_confirmed['error']['confirmation_required']
        status, path_rejected = self.request('POST', '/api/images/register', {'path': 'image.yaml', 'confirmed': True})
        assert status == 400
        assert 'manifest_text' in path_rejected['error']['message']
        status, registered = self.request('POST', '/api/images/register', {'manifest_text': manifest, 'source': 'gui-yaml-agent.yaml', 'confirmed': True})
        assert status == 200
        assert registered['image_id'] == 'gui-yaml-agent:v0'
        status, listed = self.request('GET', '/api/images')
        assert status == 200
        assert 'gui-yaml-agent:v0' in {item['image_id'] for item in listed}

    def test_scheduler_requests_are_serialized(self) -> None:
        first_status, first = self.request('POST', '/api/processes', {'goal': 'goal', 'auto_run': False})
        assert first_status == 200
        pid = first['pid']
        self.server.service.scheduler.running = True
        status, duplicate = self.request('POST', f'/api/processes/{pid}/run', {'max_quanta': 1})
        assert status == 200
        assert duplicate['running']
        self.server.service.scheduler.running = False

    def test_process_run_targets_selected_process(self) -> None:
        _first_status, first = self.request('POST', '/api/processes', {'goal': 'first', 'auto_run': False})
        _second_status, second = self.request('POST', '/api/processes', {'goal': 'second', 'auto_run': False})
        seen: list[str] = []

        async def fake_quantum(pid: str) -> dict[str, str]:
            seen.append(pid)
            self.server.service.runtime.process.pause(pid, 'fake quantum completed')
            return {'pid': pid}
        self.server.service.runtime.arun_process_once = fake_quantum
        status, body = self.request('POST', f"/api/processes/{second['pid']}/run", {'max_quanta': 1})
        deadline = time.time() + 2.0
        while not seen and time.time() < deadline:
            time.sleep(0.01)
        assert status == 200
        assert body['reason'] == f"run:{second['pid']}"
        assert seen == [second['pid']]
        records = self.server.service.runtime.audit.trace()
        assert not any((record.target == f"process:{first['pid']}" and record.action == 'scheduler.run_quantum' for record in records))
        assert any((record.target == f"process:{second['pid']}" and record.action == 'scheduler.run_quantum' for record in records))

    def test_jsonrpc_register_rejects_host_file_path(self) -> None:
        status, body = self.request('POST', '/api/jsonrpc/register', {'path': 'secrets.yaml', 'confirmed': True})
        assert status == 400
        assert 'manifest_text' in body['error']['message']

    def test_jsonrpc_register_requires_manifest_text(self) -> None:
        status, body = self.request('POST', '/api/jsonrpc/register', {'confirmed': True})
        assert status == 400
        assert 'manifest_text' in body['error']['message']

    def test_invalid_max_quanta_is_rejected(self) -> None:
        status, body = self.request('POST', '/api/processes', {'goal': 'goal', 'max_quanta': 0})
        assert status == 400
        assert 'max_quanta' in body['error']['message']

    def test_request_body_size_is_bounded(self) -> None:
        status, body = self.request('POST', '/api/processes', {'goal': 'x' * 1100000})
        assert status == 413
        assert 'exceeds' in body['error']['message']

    def test_shutdown_endpoint_stops_http_server(self) -> None:
        try:
            status, body = self.request('POST', '/api/shutdown', {})
            assert status == 200
            assert body['status'] == 'shutting_down'
        except ConnectionResetError:
            pass
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()
        self.server.service.shutdown()

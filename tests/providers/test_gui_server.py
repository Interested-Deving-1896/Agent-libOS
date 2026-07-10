from __future__ import annotations
import pytest
import http.client
import json
import tempfile
import threading
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any
from agent_libos.api.gui.server import (
    GuiEventBroadcaster,
    GuiRuntimeService,
    GuiServerError,
    _BoundedSeenKeys,
    _sse_payload_data,
    create_gui_http_server,
)
from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import AgentLibOSConfig, DEFAULT_CONFIG, GuiDefaults, RuntimeDefaults
from agent_libos.models import CapabilityRight, EventType, ObjectMetadata, ObjectType, ProcessSignal, ProcessStatus
from agent_libos.runtime.runtime import Runtime
from tests.support.skills import write_skill_package

class TestGuiServer:

    def setup_method(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.llm_profiles_file = Path(self.temp_dir.name) / 'llm-profiles.json'
        self.server = create_gui_http_server(
            db='local',
            port=0,
            token='test-token',
            auto_run=False,
            llm_profiles_file=self.llm_profiles_file,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def teardown_method(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.service.shutdown()
        self.server.server_close()
        self.temp_dir.cleanup()

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

    def request_json_text(self, method: str, path: str, raw: str) -> tuple[int, Any]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        conn.request(
            method,
            path,
            body=raw.encode('utf-8'),
            headers={'Authorization': 'Bearer test-token', 'Content-Type': 'application/json'},
        )
        response = conn.getresponse()
        data = response.read()
        conn.close()
        decoded = json.loads(data.decode('utf-8')) if data else None
        return response.status, decoded

    def test_auth_health_snapshot_and_process_flow(self) -> None:
        status, _body = self.request('GET', '/api/health', token='wrong')
        assert status == 401
        status, health = self.request('GET', '/api/health')
        assert status == 200
        assert health['ok']
        assert not health['scheduler']['auto_run']
        assert health['scheduler']['default_max_quanta'] is None
        status, _profile = self.request(
            'POST',
            '/api/llm-profiles',
            {'profile_id': 'gui-spawn', 'model': 'gui-spawn-model', 'api_key_env': 'GUI_SPAWN_API_KEY'},
        )
        assert status == 200
        status, spawned = self.request(
            'POST',
            '/api/processes',
            {'goal': 'inspect README', 'auto_run': False, 'llm_profile': 'gui-spawn'},
        )
        assert status == 200
        pid = spawned['pid']
        assert spawned['process']['llm_profile_id'] == 'gui-spawn'
        status, message = self.request('POST', f'/api/processes/{pid}/message', {'body': 'hello', 'auto_run': False})
        assert status == 200
        assert message['message']['body'] == 'hello'
        status, interrupt = self.request('POST', f'/api/processes/{pid}/interrupt', {'body': 'stop', 'auto_run': False})
        assert status == 200
        assert interrupt['message']['kind'] == 'interrupt'
        status, snapshot = self.request('GET', '/api/snapshot')
        assert status == 200
        assert len(snapshot['processes']) == 1
        assert snapshot['processes'][0]['llm_profile_id'] == 'gui-spawn'
        assert snapshot['processes'][0]['unread_message_count'] >= 2
        assert 'tools' in snapshot
        assert 'images' in snapshot
        assert any((profile['profile_id'] == 'gui-spawn' for profile in snapshot['llm_profiles']))
        assert any((image['image_id'] == 'base-agent:v0' for image in snapshot['images']))

    def test_llm_profile_endpoints_persist_user_profiles_and_reject_secrets(self, monkeypatch) -> None:
        monkeypatch.setenv('KIMI_API_KEY', 'secret')

        status, profiles = self.request('GET', '/api/llm-profiles')
        assert status == 200
        assert any(profile['profile_id'] == 'default' and profile['source'] == 'config' for profile in profiles)

        status, created = self.request(
            'POST',
            '/api/llm-profiles',
            {
                'profile_id': 'kimi-k2.7-code',
                'model': 'kimi-k2.7-code',
                'base_url': 'https://kimi.example/v1',
                'api_key_env': 'KIMI_API_KEY',
                'api_mode': 'chat',
                'temperature': 0.1,
                'store': None,
                'responses_previous_response_id': None,
                'parallel_tool_calls': None,
                'auto_wait_on_empty_tool_calls': None,
                'allow_custom_base_url': None,
            },
        )
        assert status == 200
        assert created['profile_id'] == 'kimi-k2.7-code'
        assert created['source'] == 'user'
        assert created['editable'] is True
        assert created['api_key_env_present'] is True
        assert created['store'] is None
        assert created['parallel_tool_calls'] is None
        assert created['auto_wait_on_empty_tool_calls'] is None
        assert created['allow_custom_base_url'] is False

        status, updated = self.request(
            'PUT',
            '/api/llm-profiles/kimi-k2.7-code',
            {
                'model': 'kimi-k2.7-code',
                'base_url': 'https://kimi.example/v1/',
                'api_key_env': 'KIMI_API_KEY',
                'api_mode': 'chat',
                'max_tokens': 4096,
                'allow_custom_base_url': True,
            },
        )
        assert status == 200
        assert updated['max_tokens'] == 4096
        assert updated['allow_custom_base_url'] is True
        assert 'secret' not in self.llm_profiles_file.read_text(encoding='utf-8')

        status, rejected = self.request(
            'POST',
            '/api/llm-profiles',
            {'profile_id': 'bad-secret', 'model': 'bad', 'api_key_env': 'BAD_API_KEY', 'api_key': 'secret'},
        )
        assert status == 400
        assert 'API keys are not accepted' in rejected['error']['message']

        self.server.service.shutdown()
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

        self.server = create_gui_http_server(
            db='local',
            port=0,
            token='test-token',
            auto_run=False,
            llm_profiles_file=self.llm_profiles_file,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        status, profiles = self.request('GET', '/api/llm-profiles')
        assert status == 200
        assert any(profile['profile_id'] == 'kimi-k2.7-code' and profile['max_tokens'] == 4096 for profile in profiles)

    def test_llm_profile_spawn_exec_validation_and_delete_in_use(self) -> None:
        status, body = self.request('POST', '/api/processes', {'goal': 'bad profile', 'auto_run': False, 'llm_profile': 'missing'})
        assert status == 400
        assert 'unknown LLM profile' in body['error']['message']

        status, _profile = self.request(
            'POST',
            '/api/llm-profiles',
            {'profile_id': 'glm-5.2', 'model': 'glm-5.2', 'api_key_env': 'GLM_API_KEY'},
        )
        assert status == 200
        status, spawned = self.request('POST', '/api/processes', {'goal': 'profile', 'auto_run': False, 'llm_profile': 'glm-5.2'})
        assert status == 200
        pid = spawned['pid']
        status, body = self.request('DELETE', '/api/llm-profiles/glm-5.2')
        assert status == 409
        assert pid in body['error']['pids']

        status, bad_exec = self.request(
            'POST',
            f'/api/processes/{pid}/exec',
            {'image': 'base-agent:v0', 'goal': 'new', 'llm_profile': 'missing', 'confirmed': True},
        )
        assert status == 400
        assert 'unknown LLM profile' in bad_exec['error']['message']

        self.server.service.runtime.process.exit(pid, message='done')
        status, deleted = self.request('DELETE', '/api/llm-profiles/glm-5.2')
        assert status == 409
        assert deleted['error']['profile_id'] == 'glm-5.2'

    def test_process_rating_endpoint_updates_snapshot_and_audit(self) -> None:
        status, spawned = self.request('POST', '/api/processes', {'goal': 'rate agent', 'auto_run': False})
        assert status == 200
        pid = spawned['pid']

        status, empty = self.request('GET', f'/api/processes/{pid}/rating')
        assert status == 200
        assert empty is None

        status, rating = self.request('POST', f'/api/processes/{pid}/rating', {'score': 5, 'comment': 'strong result'})
        assert status == 200
        assert rating['pid'] == pid
        assert rating['score'] == 5
        assert rating['comment'] == 'strong result'
        assert rating['rater'] == DEFAULT_CONFIG.runtime.default_human
        assert rating['source'] == 'gui'

        status, updated = self.request('POST', f'/api/processes/{pid}/rating', {'score': 3, 'comment': 'missed detail'})
        assert status == 200
        assert updated['rating_id'] == rating['rating_id']
        assert updated['score'] == 3

        status, snapshot = self.request('GET', '/api/snapshot')
        assert status == 200
        process = next(item for item in snapshot['processes'] if item['pid'] == pid)
        assert process['rating']['score'] == 3
        assert process['rating']['comment'] == 'missed detail'
        assert any(
            record.action == 'agent.rating.upsert'
            and record.target == f'process:{pid}'
            for record in self.server.service.runtime.audit.trace()
        )

    def test_process_rating_endpoint_rejects_invalid_requests(self) -> None:
        status, spawned = self.request('POST', '/api/processes', {'goal': 'bad rating', 'auto_run': False})
        assert status == 200
        pid = spawned['pid']

        status, body = self.request('POST', f'/api/processes/{pid}/rating', {'score': 0})
        assert status == 400
        assert 'between 1 and 5' in body['error']['message']

        status, body = self.request('GET', '/api/processes/missing/rating')
        assert status == 404
        assert 'process not found' in body['error']['message']

    def test_encoded_route_segments_are_decoded(self) -> None:
        status, inspected = self.request('GET', '/api/images/base-agent%3Av0')

        assert status == 200
        assert inspected['image']['image_id'] == 'base-agent:v0'

    def test_process_spawn_accepts_initial_working_directory(self) -> None:
        status, spawned = self.request(
            'POST',
            '/api/processes',
            {'goal': 'cwd target', 'working_directory': 'src\\app', 'auto_run': False},
        )

        assert status == 200
        assert spawned['process']['working_directory'] == 'src/app'

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

        status, headers, _body = self.request_raw(
            'OPTIONS',
            '/api/health',
            extra_headers={'Origin': 'null'},
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

    def test_sse_broadcaster_invalidates_evicted_and_restarted_cursors(self) -> None:
        broadcaster = GuiEventBroadcaster(max_events=2)
        broadcaster.publish('snapshot', {'version': 1})
        broadcaster.publish('snapshot', {'version': 2})
        broadcaster.publish('snapshot', {'version': 3})

        evicted = broadcaster.replay_after(0)

        assert [event.event for event in evicted] == ['event.invalidated', 'snapshot', 'snapshot']
        assert evicted[0].seq == 1
        assert evicted[0].data == {
            'invalidated': True,
            'reason': 'sse_cursor_not_replayable',
            'requested_cursor': 0,
            'reset_cursor': 1,
            'oldest_available': 2,
            'latest_available': 3,
        }
        assert [event.seq for event in evicted[1:]] == [2, 3]

        restarted = broadcaster.replay_after(99)

        assert [event.event for event in restarted] == ['event.invalidated', 'snapshot', 'snapshot']
        assert restarted[0].seq == 0
        assert restarted[0].data['requested_cursor'] == 99
        assert restarted[0].data['reset_cursor'] == 0

    def test_gui_delta_deduplication_is_bounded(self) -> None:
        seen = _BoundedSeenKeys(2)

        assert seen.add_if_new('first') is True
        assert seen.add_if_new('second') is True
        assert seen.add_if_new('second') is False
        assert seen.add_if_new('third') is True
        assert len(seen) == 2
        assert seen.add_if_new('first') is True
        assert len(seen) == 2

    def test_snapshot_audit_window_contains_latest_records(self) -> None:
        for index in range(205):
            self.server.service.runtime.audit.record(
                actor='test',
                action=f'audit.window.{index}',
                target='process:audit-window',
            )

        status, snapshot = self.request('GET', '/api/snapshot')
        actions = [record['action'] for record in snapshot['audit']]

        assert status == 200
        assert 'audit.window.204' in actions
        assert 'audit.window.0' not in actions

    def test_snapshot_truncates_model_amplified_event_payloads(self) -> None:
        huge = 'x' * (self.server.service.runtime.config.gui.snapshot_string_max_chars + 100)
        self.server.service.runtime.events.emit(
            EventType.EXTERNAL_WRITE,
            source='gui-test',
            target='gui-test',
            payload={'blob': huge},
        )

        status, snapshot = self.request('GET', '/api/snapshot')

        assert status == 200
        serialized = json.dumps(snapshot)
        assert huge not in serialized
        event = snapshot['events'][-1]
        assert isinstance(event['payload']['blob'], str)
        assert len(event['payload']['blob']) == self.server.service.runtime.config.gui.snapshot_string_max_chars
        truncation = {
            path: meta
            for path, meta in snapshot['_truncated'].items()
            if path.endswith('.payload.blob')
        }
        assert list(truncation.values())[0]['kind'] == 'string'
        assert list(truncation.values())[0]['chars'] == len(huge)

    def test_snapshot_array_truncation_uses_metadata_not_sentinel_items(self) -> None:
        self.server.service.runtime.config = replace(
            self.server.service.runtime.config,
            gui=replace(
                self.server.service.runtime.config.gui,
                snapshot_collection_max_items=20,
                snapshot_event_limit=25,
            ),
        )
        for index in range(25):
            self.server.service.runtime.events.emit(
                EventType.EXTERNAL_WRITE,
                source='gui-test',
                target='gui-test',
                payload={'index': index},
            )

        status, snapshot = self.request('GET', '/api/snapshot')

        assert status == 200
        assert len(snapshot['events']) == 20
        assert all('event_id' in event for event in snapshot['events'])
        assert not any(event.get('truncated') is True for event in snapshot['events'])
        assert snapshot['_truncated']['events']['kind'] == 'array'
        assert snapshot['_truncated']['events']['omitted'] == 5

    def test_oversized_snapshot_sse_payload_uses_explicit_truncated_event(self) -> None:
        event_name, payload = _sse_payload_data(
            'snapshot',
            {'snapshot': {'events': [{'payload': 'x' * 100}]}},
            max_bytes=50,
            string_limit=200,
            collection_limit=200,
        )

        assert event_name == 'snapshot_truncated'
        assert payload['invalidated'] is True
        assert payload['event'] == 'snapshot'

    def test_strict_json_bool_rejects_string_false(self) -> None:
        status, body = self.request('POST', '/api/processes', {'goal': 'strict bool', 'auto_run': 'false'})

        assert status == 400
        assert 'auto_run must be a JSON boolean' in body['error']['message']

    def test_process_audit_filters_before_limit(self) -> None:
        _status, spawned = self.request('POST', '/api/processes', {'goal': 'audit target', 'auto_run': False})
        pid = spawned['pid']
        self.server.service.runtime.audit.record(
            actor=pid,
            action='process.audit.target',
            target=f'process:{pid}',
        )
        for index in range(205):
            self.server.service.runtime.audit.record(
                actor='noise',
                action=f'process.audit.noise.{index}',
                target='process:noise',
            )

        status, records = self.request('GET', f'/api/processes/{pid}/audit?limit=1')

        assert status == 200
        assert [record['action'] for record in records] == ['process.audit.target']

    def test_high_risk_exec_requires_confirmation(self) -> None:
        status, _profile = self.request(
            'POST',
            '/api/llm-profiles',
            {'profile_id': 'gui-exec', 'model': 'gui-exec-model', 'api_key_env': 'GUI_EXEC_API_KEY'},
        )
        assert status == 200
        _status, spawned = self.request('POST', '/api/processes', {'goal': 'goal', 'auto_run': False})
        pid = spawned['pid']
        status, denied = self.request(
            'POST',
            f'/api/processes/{pid}/exec',
            {'image': 'base-agent:v0', 'goal': 'new', 'llm_profile': 'gui-exec'},
        )
        assert status == 409
        assert denied['error']['confirmation_required']
        assert denied['error']['preview']['llm_profile'] == 'gui-exec'
        status, string_confirmed = self.request('POST', f'/api/processes/{pid}/exec', {'image': 'base-agent:v0', 'goal': 'new', 'confirmed': 'true'})
        assert status == 400
        assert 'confirmed must be a JSON boolean' in string_confirmed['error']['message']
        status, allowed = self.request(
            'POST',
            f'/api/processes/{pid}/exec',
            {
                'image': 'base-agent:v0',
                'goal': 'new',
                'confirmed': True,
                'auto_run': False,
                'llm_profile': 'gui-exec',
            },
        )
        assert status == 200
        assert allowed['process']['image_id'] == 'base-agent:v0'
        assert allowed['process']['llm_profile_id'] == 'gui-exec'

    def test_destructive_process_signal_requires_confirmation(self) -> None:
        _status, spawned = self.request('POST', '/api/processes', {'goal': 'signal target', 'auto_run': False})
        pid = spawned['pid']

        status, denied = self.request('POST', f'/api/processes/{pid}/signal', {'signal': ProcessSignal.TERMINATE.value})
        assert status == 409
        assert denied['error']['confirmation_required']
        assert denied['error']['preview']['signal'] == ProcessSignal.TERMINATE.value

        status, string_confirmed = self.request(
            'POST',
            f'/api/processes/{pid}/signal',
            {'signal': ProcessSignal.TERMINATE.value, 'confirmed': 'true'},
        )
        assert status == 400
        assert 'confirmed must be a JSON boolean' in string_confirmed['error']['message']

        status, allowed = self.request(
            'POST',
            f'/api/processes/{pid}/signal',
            {'signal': ProcessSignal.TERMINATE.value, 'confirmed': True},
        )
        assert status == 200
        assert allowed['status'] == ProcessStatus.KILLED.value

    def test_invalid_process_signal_is_a_bad_request_without_mutation(self) -> None:
        _status, spawned = self.request('POST', '/api/processes', {'goal': 'invalid signal target', 'auto_run': False})
        pid = spawned['pid']

        status, body = self.request('POST', f'/api/processes/{pid}/signal', {'signal': 'not-a-signal'})

        assert status == 400
        assert 'unknown process signal' in body['error']['message']
        assert self.server.service.runtime.process.get(pid).status == ProcessStatus.RUNNABLE

    def test_missing_required_mutation_fields_are_bad_requests(self) -> None:
        _status, spawned = self.request('POST', '/api/processes', {'goal': 'required fields', 'auto_run': False})
        pid = spawned['pid']

        exec_status, exec_body = self.request(
            'POST',
            f'/api/processes/{pid}/exec',
            {'goal': 'missing image', 'confirmed': True, 'auto_run': False},
        )
        cd_status, cd_body = self.request('POST', f'/api/processes/{pid}/cd', {})
        checkpoint_status, checkpoint_body = self.request('POST', '/api/checkpoints/create', {})

        assert exec_status == 400
        assert 'image must be a non-empty JSON string' in exec_body['error']['message']
        assert cd_status == 400
        assert 'path must be a non-empty JSON string' in cd_body['error']['message']
        assert checkpoint_status == 400
        assert 'pid must be a non-empty JSON string' in checkpoint_body['error']['message']
        assert self.server.service.runtime.process.get(pid).image_id == 'base-agent:v0'

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

    def test_capability_actor_mode_enforces_process_authority(self) -> None:
        runtime = self.server.service.runtime
        _status, actor = self.request('POST', '/api/processes', {'goal': 'capability actor', 'auto_run': False})
        _status, subject = self.request('POST', '/api/processes', {'goal': 'capability subject', 'auto_run': False})

        status, denied = self.request(
            'POST',
            '/api/capabilities/grant',
            {
                'subject': subject['pid'],
                'resource': 'object:gui-actor-grant',
                'rights': ['read'],
                'actor': actor['pid'],
                'confirmed': True,
            },
        )
        assert status == 403
        assert 'lacks grant/admin authority' in denied['error']['message']

        status, spoofed = self.request(
            'POST',
            '/api/capabilities/grant',
            {
                'subject': subject['pid'],
                'resource': 'object:gui-spoofed-human-grant',
                'rights': ['read'],
                'actor': DEFAULT_CONFIG.runtime.default_human_actor,
                'confirmed': True,
            },
        )
        assert status == 403
        assert 'lacks grant/admin authority' in spoofed['error']['message']

        status, admin_granted = self.request(
            'POST',
            '/api/capabilities/grant',
            {
                'subject': subject['pid'],
                'resource': 'object:gui-admin-grant',
                'rights': ['read'],
                'confirmed': True,
            },
        )
        assert status == 200
        assert admin_granted['subject'] == subject['pid']

        runtime.capability.grant(actor['pid'], 'object:gui-actor-grant', [CapabilityRight.READ], issued_by='test')
        runtime.capability.grant(actor['pid'], 'object:gui-actor-grant', [CapabilityRight.GRANT], issued_by='test')
        status, granted = self.request(
            'POST',
            '/api/capabilities/grant',
            {
                'subject': subject['pid'],
                'resource': 'object:gui-actor-grant',
                'rights': ['read'],
                'actor': actor['pid'],
                'confirmed': True,
            },
        )
        assert status == 200
        assert granted['subject'] == subject['pid']
        assert granted['parent_cap_id']

        runtime.capability.grant(actor['pid'], 'object:gui-delegate', [CapabilityRight.READ], issued_by='test', delegable=True)
        status, mismatched_parent = self.request(
            'POST',
            '/api/capabilities/delegate',
            {
                'parent': subject['pid'],
                'child': actor['pid'],
                'resource': 'object:gui-delegate',
                'rights': ['read'],
                'actor': actor['pid'],
                'confirmed': True,
            },
        )
        assert status == 403
        assert 'actor-mode delegation' in mismatched_parent['error']['message']

        status, delegated = self.request(
            'POST',
            '/api/capabilities/delegate',
            {
                'parent': actor['pid'],
                'child': subject['pid'],
                'resource': 'object:gui-delegate',
                'rights': ['read'],
                'actor': actor['pid'],
                'confirmed': True,
            },
        )
        assert status == 200
        assert delegated['subject'] == subject['pid']

        cap = runtime.capability.grant(subject['pid'], 'object:gui-revoke', [CapabilityRight.READ], issued_by='test')
        status, revoke_denied = self.request(
            'POST',
            f"/api/capabilities/{cap.cap_id}/revoke",
            {'actor': actor['pid'], 'confirmed': True},
        )
        assert status == 403
        assert 'lacks revoke/admin authority' in revoke_denied['error']['message']

        runtime.capability.grant(actor['pid'], 'object:gui-revoke', [CapabilityRight.REVOKE], issued_by='test')
        status, revoked = self.request(
            'POST',
            f"/api/capabilities/{cap.cap_id}/revoke",
            {'actor': actor['pid'], 'confirmed': True},
        )
        assert status == 200
        assert revoked['status'] == 'revoked'

    def test_image_register_accepts_package_files_and_rejects_host_file_path(self) -> None:
        files = _gui_image_package_files()
        status, denied = self.request('POST', '/api/images/register', {'files': files, 'source': 'gui-package-agent'})
        assert status == 409
        assert denied['error']['confirmation_required']
        status, string_confirmed = self.request('POST', '/api/images/register', {'files': files, 'source': 'gui-package-agent', 'confirmed': 'true'})
        assert status == 400
        assert 'confirmed must be a JSON boolean' in string_confirmed['error']['message']
        status, path_rejected = self.request('POST', '/api/images/register', {'path': 'image-package', 'confirmed': True})
        assert status == 400
        assert 'package files' in path_rejected['error']['message']
        status, registered = self.request('POST', '/api/images/register', {'files': files, 'source': 'gui-package-agent', 'confirmed': True})
        assert status == 200
        assert registered['image_id'] == 'gui-package-agent:v0'
        assert registered['boot']['kind'] == 'image_package'
        status, listed = self.request('GET', '/api/images')
        assert status == 200
        assert 'gui-package-agent:v0' in {item['image_id'] for item in listed}

    def test_scheduler_requests_are_serialized(self) -> None:
        first_status, first = self.request('POST', '/api/processes', {'goal': 'goal', 'auto_run': False})
        assert first_status == 200
        pid = first['pid']
        self.server.service.scheduler.running = True
        status, duplicate = self.request('POST', f'/api/processes/{pid}/run', {'max_quanta': 1})
        assert status == 200
        assert duplicate['running']
        self.server.service.scheduler.running = False

    def test_scheduler_background_releases_runtime_lock_between_quanta(self) -> None:
        calls: list[tuple[int | None, bool]] = []

        def fake_run_until_idle(
            *,
            max_quanta: int | None = None,
            process_human_queue: bool = True,
        ) -> list[dict[str, int]]:
            calls.append((max_quanta, process_human_queue))
            return [{'call': len(calls)}] if len(calls) == 1 else []

        self.server.service.runtime.run_until_idle = fake_run_until_idle

        status = self.server.service.scheduler.start(max_quanta=3, reason='test-batch')
        assert status['running']
        thread = self.server.service.scheduler._thread
        assert thread is not None
        thread.join(timeout=2)

        assert calls == [(1, False), (1, False)]
        assert self.server.service.scheduler.status()['last_result'] == [{'call': 1}]

    def test_health_uses_fast_path_when_runtime_lock_is_busy(self) -> None:
        self.server.service.runtime_lock.acquire()
        try:
            status, health = self.request('GET', '/api/health')
        finally:
            self.server.service.runtime_lock.release()

        assert status == 200
        assert health['runtime_busy'] is True
        assert health['process_count'] is None

    def test_gui_shutdown_waits_for_runtime_users_and_can_retry_after_timeout(self) -> None:
        runtime = Runtime.open('local')
        service = GuiRuntimeService(runtime=runtime, auto_run=False, token='lifecycle-test')
        entered = threading.Event()
        release = threading.Event()
        worker_done = threading.Event()

        def runtime_user() -> None:
            with service.runtime_user():
                entered.set()
                release.wait(timeout=2.0)
            worker_done.set()

        worker = threading.Thread(target=runtime_user)
        worker.start()
        assert entered.wait(timeout=2.0)
        try:
            assert service.shutdown(timeout_s=0.01) is False
            assert not service._closed
            release.set()
            assert worker_done.wait(timeout=2.0)
            assert service.shutdown(timeout_s=1.0) is True
            assert service._closed
            assert runtime.process.list() == []
        finally:
            release.set()
            worker.join(timeout=2.0)
            service.shutdown(timeout_s=1.0)
            runtime.close()

    def test_owned_runtime_partial_shutdown_never_reopens_api(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = GuiRuntimeService(db='local', auto_run=False, token='partial-shutdown')
        original_shutdown = service.runtime.shutdown
        calls = 0

        def fail_once(*, actor: str, reason: str) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return {'ok': False, 'object_tasks_stopped': False}
            return original_shutdown(actor=actor, reason=reason)

        monkeypatch.setattr(service.runtime, 'shutdown', fail_once)
        try:
            assert service.shutdown(timeout_s=1.0) is False
            assert service._closing is True
            assert service._closed is False
            with pytest.raises(GuiServerError, match='shutting down'):
                with service.runtime_user():
                    pass

            assert service.shutdown(timeout_s=1.0) is True
            assert service._closed is True
            assert calls == 2
        finally:
            service.shutdown(timeout_s=1.0)

    def test_process_run_targets_selected_process(self) -> None:
        _first_status, first = self.request('POST', '/api/processes', {'goal': 'first', 'auto_run': False})
        _second_status, second = self.request('POST', '/api/processes', {'goal': 'second', 'auto_run': False})
        seen: list[str] = []
        seen_event = threading.Event()

        async def fake_quantum(pid: str) -> dict[str, str]:
            seen.append(pid)
            self.server.service.runtime.process.pause(pid, 'fake quantum completed')
            seen_event.set()
            return {'pid': pid}
        self.server.service.runtime.arun_process_once = fake_quantum
        status, body = self.request('POST', f"/api/processes/{second['pid']}/run", {'max_quanta': 1})
        assert seen_event.wait(timeout=2.0)
        assert status == 200
        assert body['reason'] == f"run:{second['pid']}"
        assert seen == [second['pid']]
        records = self.server.service.runtime.audit.trace()
        assert not any((record.target == f"process:{first['pid']}" and record.action == 'scheduler.run_quantum' for record in records))
        assert any((record.target == f"process:{second['pid']}" and record.action == 'scheduler.run_quantum' for record in records))

    def test_process_step_returns_and_publishes_final_scheduler_status(self) -> None:
        _status, spawned = self.request('POST', '/api/processes', {'goal': 'step once', 'auto_run': False})
        pid = spawned['pid']

        async def fake_quantum(selected_pid: str) -> dict[str, str]:
            assert selected_pid == pid
            return {'pid': selected_pid, 'status': 'completed'}

        self.server.service.runtime.arun_process_once = fake_quantum
        before = self.server.service.broadcaster.replay_after(0)[-1].seq

        status, body = self.request('POST', f'/api/processes/{pid}/step', {})

        assert status == 200
        assert body['started'] is True
        assert body['scheduler']['running'] is False
        snapshots = [
            event.data['snapshot']
            for event in self.server.service.broadcaster.replay_after(before)
            if event.event == 'snapshot'
        ]
        assert snapshots
        assert snapshots[-1]['scheduler']['running'] is False

    def test_workflow_run_endpoint_returns_result_and_snapshot_process(self) -> None:
        status, result = self.request('POST', '/api/workflows/run', {'tool': 'get_working_directory', 'args': {}})

        assert status == 200
        assert result['ok'] is True
        assert result['tool'] == 'get_working_directory'
        assert result['status'] == 'exited'
        assert result['result_oid'] is not None
        status, snapshot = self.request('GET', '/api/snapshot')
        assert status == 200
        processes = {process['pid']: process for process in snapshot['processes']}
        assert processes[result['pid']]['status'] == 'exited'

    def test_side_effect_workflow_requires_confirmation(self) -> None:
        status, denied = self.request('POST', '/api/workflows/run', {'tool': 'ask_human', 'args': {'question': 'Continue?'}})

        assert status == 409
        assert denied['error']['confirmation_required']
        assert denied['error']['action'] == 'workflow.run'
        assert denied['error']['preview']['tool'] == 'ask_human'

    def test_unknown_workflow_tool_requires_confirmation_fail_closed(self) -> None:
        status, denied = self.request('POST', '/api/workflows/run', {'tool': 'missing_workflow_tool', 'args': {}})

        assert status == 409
        assert denied['error']['confirmation_required']
        assert denied['error']['action'] == 'workflow.run'

    def test_object_task_endpoint_runs_task_and_exposes_snapshot(self) -> None:
        status, spawned = self.request('POST', '/api/processes', {'goal': 'object task', 'auto_run': False})
        assert status == 200
        pid = spawned['pid']
        self.server.service.runtime.capability.grant(pid, 'process:spawn', [CapabilityRight.WRITE], issued_by='test')
        owner = self.server.service.runtime.memory.create_object(
            pid,
            ObjectType.ARTIFACT,
            {'name': 'owner'},
            metadata=ObjectMetadata(title='owner'),
            immutable=False,
        )

        status, started = self.request(
            'POST',
            '/api/object-tasks/start',
            {
                'pid': pid,
                'owner_oid': owner.oid,
                'tool': 'get_working_directory',
                'args': {},
                'owner_watch': True,
                'watch_events': ['updated'],
                'watch_channel': 'owner-watch',
            },
        )
        assert status == 200
        assert started['owner_watch']['enabled'] is True
        assert started['owner_watch']['events'] == ['updated']
        assert started['owner_watch']['channel'] == 'owner-watch'
        status, waited = self.request('POST', f"/api/object-tasks/{started['task_id']}/wait", {'pid': pid, 'timeout_s': 2})
        assert status == 200
        assert waited['status'] == 'succeeded'
        assert waited['result_oid'] is not None
        status, snapshot = self.request('GET', '/api/snapshot')
        assert status == 200
        assert any(
            task['task_id'] == started['task_id']
            and task['status'] == 'succeeded'
            and task['owner_watch']['enabled'] is True
            for task in snapshot['object_tasks']
        )

    def test_object_task_watch_owner_endpoint_updates_existing_task(self) -> None:
        status, spawned = self.request('POST', '/api/processes', {'goal': 'object task watch', 'auto_run': False})
        assert status == 200
        pid = spawned['pid']
        self.server.service.runtime.capability.grant(pid, 'process:spawn', [CapabilityRight.WRITE], issued_by='test')
        owner = self.server.service.runtime.memory.create_object(
            pid,
            ObjectType.ARTIFACT,
            {'name': 'owner'},
            metadata=ObjectMetadata(title='owner'),
            immutable=False,
        )
        status, started = self.request(
            'POST',
            '/api/object-tasks/start',
            {'pid': pid, 'owner_oid': owner.oid, 'tool': 'receive_process_messages', 'args': {'channel': 'owner-watch'}},
        )
        assert status == 200
        status, waited = self.request('POST', f"/api/object-tasks/{started['task_id']}/wait", {'pid': pid, 'timeout_s': 2})
        assert status == 200
        assert waited['status'] == 'waiting_message'

        status, watched = self.request(
            'POST',
            f"/api/object-tasks/{started['task_id']}/watch-owner",
            {
                'pid': pid,
                'enabled': True,
                'watch_events': ['updated'],
                'watch_channel': 'owner-watch',
                'watch_kind': 'interrupt',
            },
        )

        assert status == 200
        assert watched['owner_watch']['enabled'] is True
        assert watched['owner_watch']['events'] == ['updated']
        assert watched['owner_watch']['channel'] == 'owner-watch'
        assert watched['owner_watch']['kind'] == 'interrupt'

    def test_object_task_start_rejects_invalid_watch_kind_as_bad_request(self) -> None:
        status, spawned = self.request('POST', '/api/processes', {'goal': 'bad watch kind', 'auto_run': False})
        assert status == 200
        pid = spawned['pid']
        owner = self.server.service.runtime.memory.create_object(
            pid,
            ObjectType.ARTIFACT,
            {'name': 'owner'},
            metadata=ObjectMetadata(title='owner'),
            immutable=False,
        )

        status, body = self.request(
            'POST',
            '/api/object-tasks/start',
            {
                'pid': pid,
                'owner_oid': owner.oid,
                'tool': 'get_working_directory',
                'args': {},
                'owner_watch': True,
                'watch_kind': 'bad-kind',
            },
        )

        assert status == 400
        assert 'owner watch kind' in body['error']['message']

    def test_object_task_wait_uses_bounded_timeout(self) -> None:
        seen: list[float | None] = []

        def fake_wait(task_id: str, *, actor_pid: str | None = None, timeout: float | None = None) -> dict[str, object]:
            seen.append(timeout)
            return {'task_id': task_id, 'actor_pid': actor_pid, 'timeout': timeout, 'status': 'running'}

        self.server.service.runtime.object_tasks.wait = fake_wait  # type: ignore[method-assign]

        status, body = self.request('POST', '/api/object-tasks/task-1/wait', {'pid': 'pid-1'})
        assert status == 200
        assert body['timeout'] == DEFAULT_CONFIG.gui.object_task_wait_default_timeout_s
        assert seen == [DEFAULT_CONFIG.gui.object_task_wait_default_timeout_s]

        status, body = self.request('POST', '/api/object-tasks/task-1/wait', {'timeout_s': 'nan'})
        assert status == 400
        assert 'finite' in body['error']['message']

        status, body = self.request(
            'POST',
            '/api/object-tasks/task-1/wait',
            {'timeout_s': DEFAULT_CONFIG.gui.object_task_wait_max_timeout_s + 1},
        )
        assert status == 400
        assert 'at most' in body['error']['message']

    def test_injected_runtime_config_controls_spawn_and_wait_defaults(self) -> None:
        config = AgentLibOSConfig(
            runtime=RuntimeDefaults(default_image_id='gui-base:v0', coding_image_id='gui-coding:v0'),
            gui=replace(DEFAULT_CONFIG.gui, object_task_wait_default_timeout_s=0.25, object_task_wait_max_timeout_s=0.5),
        )
        runtime = Runtime.open(config=config)
        server = create_gui_http_server(runtime=runtime, port=0, token='custom-token', auto_run=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        seen: list[float | None] = []

        def fake_wait(task_id: str, *, actor_pid: str | None = None, timeout: float | None = None) -> dict[str, object]:
            seen.append(timeout)
            return {'task_id': task_id, 'actor_pid': actor_pid, 'timeout': timeout, 'status': 'running'}

        server.service.runtime.object_tasks.wait = fake_wait  # type: ignore[method-assign]
        thread.start()
        try:
            status, spawned = _request_to_server(server, 'POST', '/api/processes', {'goal': 'custom', 'auto_run': False}, token='custom-token')
            assert status == 200
            assert spawned['process']['image_id'] == 'gui-base:v0'

            status, body = _request_to_server(server, 'POST', '/api/object-tasks/task-1/wait', {'pid': spawned['pid']}, token='custom-token')
            assert status == 200
            assert body['timeout'] == 0.25
            assert seen == [0.25]

            status, body = _request_to_server(server, 'POST', '/api/object-tasks/task-1/wait', {'timeout_s': 0.75}, token='custom-token')
            assert status == 400
            assert '0.5 seconds' in body['error']['message']
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.service.shutdown()
            server.server_close()

    def test_config_argument_controls_gui_runtime_defaults(self) -> None:
        config = AgentLibOSConfig(
            runtime=RuntimeDefaults(
                local_store_target='gui-memory',
                default_image_id='configured-gui-base:v0',
                coding_image_id='configured-gui-coding:v0',
            ),
            gui=replace(DEFAULT_CONFIG.gui, object_task_wait_default_timeout_s=0.2, object_task_wait_max_timeout_s=0.4),
        )
        server = create_gui_http_server(config=config, port=0, token='custom-token', auto_run=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        seen: list[float | None] = []

        def fake_wait(task_id: str, *, actor_pid: str | None = None, timeout: float | None = None) -> dict[str, object]:
            seen.append(timeout)
            return {'task_id': task_id, 'actor_pid': actor_pid, 'timeout': timeout, 'status': 'running'}

        server.service.runtime.object_tasks.wait = fake_wait  # type: ignore[method-assign]
        thread.start()
        try:
            assert server.service.db == 'gui-memory'
            assert server.service.runtime.store.path == 'gui-memory'

            status, spawned = _request_to_server(server, 'POST', '/api/processes', {'goal': 'custom', 'auto_run': False}, token='custom-token')
            assert status == 200
            assert spawned['process']['image_id'] == 'configured-gui-base:v0'

            status, body = _request_to_server(server, 'POST', '/api/object-tasks/task-1/wait', {'pid': spawned['pid']}, token='custom-token')
            assert status == 200
            assert body['timeout'] == 0.2
            assert seen == [0.2]
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.service.shutdown()
            server.server_close()

    def test_gui_runtime_service_redacts_postgres_dsn_in_status_payloads(self) -> None:
        dsn = 'postgresql://agent:secret@localhost:5432/agent_libos'
        runtime = Runtime.open('local')
        server = create_gui_http_server(runtime=runtime, db=dsn, port=0, token='custom-token', auto_run=False)
        try:
            redacted = 'postgresql://agent:***@localhost:5432/agent_libos'
            assert server.service.db == redacted
            assert server.service.health()['db'] == redacted
            assert server.service.snapshot()['db'] == redacted
        finally:
            server.service.shutdown()
            server.server_close()
            runtime.close()

    def test_gui_runtime_service_uses_configured_postgres_dsn_when_db_is_omitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = AgentLibOSConfig(
            runtime=RuntimeDefaults(
                store_backend='postgres',
                store_dsn='postgresql://agent:secret@localhost:5432/agent_libos',
            )
        )
        calls: dict[str, object] = {}
        original_open = Runtime.open

        def fake_open(target: object = None, **kwargs: object) -> Runtime:
            calls['target'] = target
            calls['config'] = kwargs.get('config')
            return original_open('local')

        monkeypatch.setattr(Runtime, 'open', staticmethod(fake_open))
        server = create_gui_http_server(config=config, port=0, token='custom-token', auto_run=False)
        try:
            redacted = 'postgresql://agent:***@localhost:5432/agent_libos'

            assert calls['target'] is None
            assert server.service.db == redacted
            assert server.service.health()['db'] == redacted
            assert server.service.snapshot()['db'] == redacted
        finally:
            server.service.shutdown()
            server.server_close()

    def test_injected_runtime_config_controls_request_body_limit(self) -> None:
        runtime = Runtime.open(config=AgentLibOSConfig(gui=GuiDefaults(request_body_max_bytes=8)))
        server = create_gui_http_server(runtime=runtime, port=0, token='custom-token', auto_run=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = _request_to_server(server, 'POST', '/api/scheduler/auto', {'enabled': True}, token='custom-token')
            assert status == 413
            assert '8 bytes' in body['error']['message']
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.service.shutdown()
            server.server_close()

    def test_jsonrpc_register_rejects_host_file_path(self) -> None:
        status, body = self.request('POST', '/api/jsonrpc/register', {'path': 'secrets.yaml', 'confirmed': True})
        assert status == 400
        assert 'manifest_text' in body['error']['message']

    def test_jsonrpc_register_requires_manifest_text(self) -> None:
        status, body = self.request('POST', '/api/jsonrpc/register', {'confirmed': True})
        assert status == 400
        assert 'manifest_text' in body['error']['message']

    def test_jsonrpc_register_actor_mode_requires_endpoint_write_capability(self) -> None:
        _status, spawned = self.request('POST', '/api/processes', {'goal': 'jsonrpc actor', 'auto_run': False})
        pid = spawned['pid']
        manifest = _gui_jsonrpc_manifest('gui-actor-jsonrpc')

        status, denied = self.request(
            'POST',
            '/api/jsonrpc/register',
            {'manifest_text': manifest, 'actor': pid, 'confirmed': True},
        )

        assert status == 403
        assert 'jsonrpc_endpoint:gui-actor-jsonrpc' in denied['error']['message']

        self.server.service.runtime.capability.grant(
            pid,
            'jsonrpc_endpoint:gui-actor-jsonrpc',
            [CapabilityRight.WRITE],
            issued_by='test',
        )
        status, registered = self.request(
            'POST',
            '/api/jsonrpc/register',
            {'manifest_text': manifest, 'actor': pid, 'confirmed': True},
        )

        assert status == 200
        assert registered['endpoint_id'] == 'gui-actor-jsonrpc'

    def test_mcp_register_rejects_host_file_path(self) -> None:
        status, body = self.request('POST', '/api/mcp/register', {'path': 'secrets.yaml', 'confirmed': True})
        assert status == 400
        assert 'manifest_text' in body['error']['message']

    def test_mcp_register_actor_mode_requires_server_write_capability(self) -> None:
        _status, spawned = self.request('POST', '/api/processes', {'goal': 'mcp actor', 'auto_run': False})
        pid = spawned['pid']
        manifest = _gui_mcp_manifest('gui-actor-mcp')

        status, denied = self.request(
            'POST',
            '/api/mcp/register',
            {'manifest_text': manifest, 'actor': pid, 'confirmed': True},
        )

        assert status == 403
        assert 'mcp_server:gui-actor-mcp' in denied['error']['message']

        self.server.service.runtime.capability.grant(
            pid,
            'mcp_server:gui-actor-mcp',
            [CapabilityRight.WRITE],
            issued_by='test',
        )
        spawn_status, spawn_denied = self.request(
            'POST',
            '/api/mcp/register',
            {'manifest_text': manifest, 'actor': pid, 'confirmed': True},
        )

        assert spawn_status == 403
        assert 'process:spawn' in spawn_denied['error']['message']

        self.server.service.runtime.capability.grant(
            pid,
            'process:spawn',
            [CapabilityRight.WRITE],
            issued_by='test',
        )
        stdio_status, stdio_denied = self.request(
            'POST',
            '/api/mcp/register',
            {'manifest_text': manifest, 'actor': pid, 'confirmed': True},
        )

        assert stdio_status == 403
        assert 'mcp_stdio' in stdio_denied['error']['message']

        self.server.service.runtime.capability.grant(
            pid,
            self.server.service.runtime.mcp.stdio_resource_for_argv('python3', ['-m', 'demo_mcp']),
            [CapabilityRight.EXECUTE],
            issued_by='test',
        )
        register_status, registered = self.request(
            'POST',
            '/api/mcp/register',
            {'manifest_text': manifest, 'actor': pid, 'confirmed': True},
        )
        tools_status, tools = self.request('GET', '/api/mcp/gui-actor-mcp/tools')

        assert register_status == 200
        assert tools_status == 200
        assert registered['server_id'] == 'gui-actor-mcp'
        assert tools['tools'][0]['tool_id'] == 'echo'
        assert tools['tools'][0]['resource'] == 'mcp:gui-actor-mcp:echo'

    def test_mcp_call_preserves_invalid_arguments_for_primitive_validation(self) -> None:
        _status, spawned = self.request('POST', '/api/processes', {'goal': 'mcp invalid args', 'auto_run': False})
        pid = spawned['pid']
        manifest = _gui_mcp_manifest('gui-invalid-args-mcp')
        self.server.service.runtime.mcp.register_server_from_yaml_text(
            manifest,
            actor='test',
            require_capability=False,
        )
        self.server.service.runtime.capability.grant(
            pid,
            'mcp:gui-invalid-args-mcp:echo',
            [CapabilityRight.READ],
            issued_by='test',
        )

        status, body = self.request(
            'POST',
            '/api/mcp/gui-invalid-args-mcp/call',
            {'pid': pid, 'tool_id': 'echo', 'arguments': [], 'confirmed': True},
        )

        assert status == 400
        assert 'arguments must be a JSON object' in body['error']['message']

    def test_skill_register_without_actor_rejects_host_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(Path(temp_dir), 'gui-host-path-skill', allowed_tools=['echo'])

            status, denied = self.request(
                'POST',
                '/api/skills/register',
                {'path': str(skill_dir), 'confirmed': True},
            )

            assert status == 400
            assert 'requires an actor' in denied['error']['message']

    def test_skill_register_actor_mode_requires_skill_write_capability(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir).resolve()
            skill_dir = write_skill_package(root, 'gui-actor-skill', allowed_tools=['echo'])
            relative_skill = skill_dir.relative_to(Path.cwd().resolve()).as_posix()
            skill_md = f'{relative_skill}/SKILL.md'
            _status, spawned = self.request('POST', '/api/processes', {'goal': 'skill actor', 'auto_run': False})
            pid = spawned['pid']

            status, denied = self.request(
                'POST',
                '/api/skills/register',
                {'path': relative_skill, 'actor': pid, 'confirmed': True},
            )

            assert status == 403
            assert 'filesystem:workspace' in denied['error']['message']

            self.server.service.runtime.filesystem.grant_path(
                pid,
                skill_md,
                [CapabilityRight.READ],
                issued_by='test',
            )
            status, denied = self.request(
                'POST',
                '/api/skills/register',
                {'path': relative_skill, 'actor': pid, 'confirmed': True},
            )

            assert status == 409
            assert denied['error']['type'] == 'HumanApprovalRequired'
            assert denied['error']['request_id']

            self.server.service.runtime.capability.grant(
                pid,
                'skill:gui-actor-skill',
                [CapabilityRight.WRITE],
                issued_by='test',
            )
            status, registered = self.request(
                'POST',
                '/api/skills/register',
                {'path': relative_skill, 'actor': pid, 'confirmed': True},
            )

            assert status == 200
            assert registered['skill_id'] == 'gui-actor-skill'

    def test_human_request_respond_rejects_non_pending_request(self) -> None:
        runtime = self.server.service.runtime
        pid = runtime.process.spawn(image='base-agent:v0', goal='gui human conflict')
        request_id = runtime.human.ask(pid, 'Approve once?', blocking=True)
        status, approved = self.request(
            'POST',
            f'/api/human-requests/{request_id}/respond',
            {'approved': True, 'answer': 'yes', 'auto_run': False},
        )
        status_again, conflict = self.request(
            'POST',
            f'/api/human-requests/{request_id}/respond',
            {'approved': True, 'answer': 'again', 'auto_run': False},
        )

        assert status == 200
        assert approved['request']['status'] == 'approved'
        assert status_again == 409
        assert 'not pending' in conflict['error']['message']

    def test_permission_response_requires_explicit_valid_policy(self) -> None:
        runtime = self.server.service.runtime
        pid = runtime.process.spawn(image='base-agent:v0', goal='typed gui permission')
        resource = runtime.filesystem.resource_for('agent_outputs/typed-gui.txt')
        request_id = runtime.human.query(
            pid=pid,
            human=DEFAULT_CONFIG.runtime.default_human,
            request={
                'type': 'permission_request',
                'question': 'Allow write?',
                'requested_permission': {
                    'subject': pid,
                    'resource': resource,
                    'rights': ['write'],
                    'constraints': {},
                },
            },
            blocking=True,
        )

        missing_status, missing = self.request(
            'POST',
            f'/api/human-requests/{request_id}/respond',
            {'approved': True, 'auto_run': False},
        )
        invalid_status, invalid = self.request(
            'POST',
            f'/api/human-requests/{request_id}/respond',
            {'approved': True, 'decision': {'policy': 'sometimes'}, 'auto_run': False},
        )

        assert missing_status == 400
        assert 'policy' in missing['error']['message']
        assert invalid_status == 400
        assert 'policy' in invalid['error']['message']
        assert runtime.human.get(request_id).status.value == 'pending'

        approved_status, approved = self.request(
            'POST',
            f'/api/human-requests/{request_id}/respond',
            {
                'approved': True,
                'decision': {'policy': CapabilityManager.ASK_EACH_TIME},
                'auto_run': False,
            },
        )
        assert approved_status == 200
        assert approved['request']['decision']['policy'] == CapabilityManager.ASK_EACH_TIME
        assert runtime.capability.permission_policy(pid, resource, CapabilityRight.WRITE) == CapabilityManager.ASK_EACH_TIME

    def test_question_response_requires_string_answer_before_commit(self) -> None:
        runtime = self.server.service.runtime
        pid = runtime.process.spawn(image='base-agent:v0', goal='typed gui question')
        request_id = runtime.human.query(
            pid=pid,
            human=DEFAULT_CONFIG.runtime.default_human,
            request={'type': 'question', 'question': 'Which region?'},
            blocking=True,
        )

        missing_status, missing = self.request(
            'POST',
            f'/api/human-requests/{request_id}/respond',
            {'approved': True, 'auto_run': False},
        )
        wrong_status, wrong = self.request(
            'POST',
            f'/api/human-requests/{request_id}/respond',
            {'approved': True, 'answer': 42, 'auto_run': False},
        )
        empty_status, empty = self.request(
            'POST',
            f'/api/human-requests/{request_id}/respond',
            {'approved': True, 'answer': '   ', 'auto_run': False},
        )

        assert missing_status == 400
        assert 'answer' in missing['error']['message']
        assert wrong_status == 400
        assert 'answer' in wrong['error']['message']
        assert empty_status == 400
        assert 'answer' in empty['error']['message']
        assert runtime.human.get(request_id).status.value == 'pending'

        accepted_status, accepted = self.request(
            'POST',
            f'/api/human-requests/{request_id}/respond',
            {'approved': True, 'answer': 'eu-west', 'auto_run': False},
        )
        assert accepted_status == 200
        assert accepted['request']['decision']['answer'] == 'eu-west'

    def test_human_request_delta_is_emitted_for_each_changed_version(self) -> None:
        runtime = self.server.service.runtime
        pid = runtime.process.spawn(image='base-agent:v0', goal='gui human delta')
        cursor = self.server.service.broadcaster.replay_after(0)[-1].seq
        request_id = runtime.human.ask(pid, 'Emit both versions?', blocking=True)

        self.server.service.publish_runtime_changes('human.pending')
        pending_events = self.server.service.broadcaster.replay_after(cursor)
        pending_updates = [
            event
            for event in pending_events
            if event.event == 'human_request.updated' and event.data['request_id'] == request_id
        ]
        assert len(pending_updates) == 1
        assert pending_updates[0].data['status'] == 'pending'
        cursor = pending_events[-1].seq

        runtime.human.approve(request_id, {'approved': True, 'answer': 'yes', 'source': 'test'})
        self.server.service.publish_runtime_changes('human.approved')
        approved_events = self.server.service.broadcaster.replay_after(cursor)
        approved_updates = [
            event
            for event in approved_events
            if event.event == 'human_request.updated' and event.data['request_id'] == request_id
        ]
        assert len(approved_updates) == 1
        assert approved_updates[0].data['status'] == 'approved'
        cursor = approved_events[-1].seq

        self.server.service.publish_runtime_changes('human.unchanged')
        unchanged = self.server.service.broadcaster.replay_after(cursor)
        assert not any(
            event.event == 'human_request.updated' and event.data['request_id'] == request_id
            for event in unchanged
        )

    def test_permission_response_without_approved_uses_explicit_deny_policy(self) -> None:
        runtime = self.server.service.runtime
        pid = runtime.process.spawn(image='base-agent:v0', goal='gui human default reject')
        request_id = runtime.human.query(
            pid=pid,
            human=DEFAULT_CONFIG.runtime.default_human,
            request={
                'type': 'permission_request',
                'question': 'Allow object read?',
                'requested_permission': {
                    'subject': pid,
                    'resource': 'object:gui-default-reject',
                    'rights': ['read'],
                },
            },
            blocking=True,
        )

        status, rejected = self.request(
            'POST',
            f'/api/human-requests/{request_id}/respond',
            {
                'decision': {'policy': CapabilityManager.ALWAYS_DENY},
                'auto_run': False,
            },
        )

        assert status == 200
        assert rejected['request']['status'] == 'rejected'
        assert (
            runtime.capability.permission_policy(pid, 'object:gui-default-reject', CapabilityRight.READ)
            == 'always_deny'
        )

    def test_invalid_max_quanta_is_rejected(self) -> None:
        before_count = len(self.server.service.runtime.process.list())
        status, body = self.request('POST', '/api/processes', {'goal': 'goal', 'max_quanta': 1.5})
        assert status == 400
        assert 'max_quanta' in body['error']['message']
        assert len(self.server.service.runtime.process.list()) == before_count

        status, body = self.request('POST', '/api/processes', {'goal': 'goal', 'max_quanta': 0})
        assert status == 400
        assert 'max_quanta' in body['error']['message']
        assert len(self.server.service.runtime.process.list()) == before_count

    def test_process_resume_validates_body_before_mutating_process(self) -> None:
        runtime = self.server.service.runtime
        pid = runtime.process.spawn(image='base-agent:v0', goal='resume validation')
        runtime.process.pause(pid, 'hold for invalid resume body')
        assert runtime.process.get(pid).status == ProcessStatus.PAUSED

        status, body = self.request_json_text('POST', f'/api/processes/{pid}/resume', '[]')

        assert status == 400
        assert 'JSON object' in body['error']['message']
        assert runtime.process.get(pid).status == ProcessStatus.PAUSED

    def test_request_body_size_is_bounded(self) -> None:
        self.server.service.runtime.config = replace(
            self.server.service.runtime.config,
            gui=replace(self.server.service.runtime.config.gui, request_body_max_bytes=1024),
        )
        status, body = self.request('POST', '/api/processes', {'goal': 'x' * 1100})
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


def _request_to_server(
    server: Any,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    token: str,
) -> tuple[int, Any]:
    host, port = server.server_address
    conn = http.client.HTTPConnection(host, port, timeout=10)
    headers = {'Authorization': f'Bearer {token}'}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    decoded = json.loads(data.decode('utf-8')) if data else None
    return response.status, decoded


def _gui_image_package_files() -> dict[str, str]:
    return {
        "IMAGE.yaml": """
image_id: gui-package-agent:v0
name: gui-package-agent
version: v0
prompt: prompt.md
default_tools:
  - human_output
""".lstrip(),
        "prompt.md": "Registered from GUI package files.\n",
    }


def _gui_jsonrpc_manifest(endpoint_id: str) -> str:
    return f"""
schema_version: 1
endpoint_id: {endpoint_id}
url: https://api.example.test/jsonrpc
methods:
  - method_id: echo
    rpc_method: echo
    right: read
    rollback_class: no_rollback_required
    state_mutation: false
    information_flow: true
""".lstrip()


def _gui_mcp_manifest(server_id: str) -> str:
    return f"""
schema_version: 1
server_id: {server_id}
transport: stdio
stdio:
  command: python3
  args: ["-m", "demo_mcp"]
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
""".lstrip()

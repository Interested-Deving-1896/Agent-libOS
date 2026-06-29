from __future__ import annotations
import pytest
from uuid import uuid4
from agent_libos import Runtime
from agent_libos.models import CapabilityRight, EventType, ForkMode, HumanRequestStatus, ProcessStatus

class TestExternalBoundary:

    def setup_method(self) -> None:
        self.runtime = Runtime.open('local')
        self.human_output: list[str] = []
        self.runtime.substrate.human.output_sink = self.human_output.append

    def teardown_method(self) -> None:
        self.runtime.close()

    def test_read_file_tool_cannot_bypass_filesystem_capability(self) -> None:
        path = self._write_workspace_fixture('hello from workspace')
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='read a file')
        denied = self.runtime.tools.call(pid, 'read_text_file', {'path': path})
        assert not denied.ok
        assert 'lacks read' in (denied.error or '')
        assert 'primitive.filesystem.read_text' not in self._audit_actions()
        self.runtime.filesystem.grant_path(pid, path, [CapabilityRight.READ], issued_by='test')
        allowed = self.runtime.tools.call(pid, 'read_text_file', {'path': path})
        assert allowed.ok
        assert allowed.payload['content'] == 'hello from workspace'
        assert 'primitive.filesystem.read_text' in self._audit_actions()

    def test_write_file_tool_cannot_bypass_filesystem_capability(self) -> None:
        path = f'agent_outputs/boundary_write_{uuid4().hex}.txt'
        target = self.runtime.workspace_root / path
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='write a file')
        denied = self.runtime.tools.call(pid, 'write_text_file', {'path': path, 'content': 'denied'})
        assert not denied.ok
        assert not target.exists()
        self.runtime.filesystem.grant_path(pid, path, [CapabilityRight.WRITE], issued_by='test')
        allowed = self.runtime.tools.call(pid, 'write_text_file', {'path': path, 'content': 'allowed'})
        assert allowed.ok
        assert target.read_text(encoding='utf-8') == 'allowed'
        assert 'primitive.filesystem.write_text' in self._audit_actions()

    def test_overwrite_false_is_atomic_create_only_at_provider_sink(self, monkeypatch: pytest.MonkeyPatch) -> None:
        path = f'agent_outputs/create_only_race_{uuid4().hex}.txt'
        target = self.runtime.workspace_root / path
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='create only race')
        self.runtime.filesystem.grant_path(pid, path, [CapabilityRight.WRITE], issued_by='test')
        created_by_racer = False

        def create_before_open(operation: str, sink_target: object) -> None:
            nonlocal created_by_racer
            if operation != 'write_text' or created_by_racer or sink_target != target:
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('racer', encoding='utf-8')
            created_by_racer = True

        monkeypatch.setattr(self.runtime.substrate.filesystem, '_before_path_sink', create_before_open)

        denied = self.runtime.tools.call(pid, 'write_text_file', {'path': path, 'content': 'new', 'overwrite': False})

        assert not denied.ok
        assert 'already exists' in (denied.error or '')
        assert target.read_text(encoding='utf-8') == 'racer'

    def test_write_precondition_does_not_leak_existing_file_without_capability(self) -> None:
        path = self._write_workspace_fixture('existing')
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='probe existing file')
        denied = self.runtime.tools.call(pid, 'write_text_file', {'path': path, 'content': 'new', 'overwrite': False})
        assert not denied.ok
        assert 'lacks write' in (denied.error or '')
        assert 'already exists' not in (denied.error or '')
        assert (self.runtime.workspace_root / path).read_text(encoding='utf-8') == 'existing'

    def test_delete_precondition_does_not_leak_missing_file_without_capability(self) -> None:
        path = f'agent_outputs/missing_delete_{uuid4().hex}.txt'
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='probe missing file')
        denied = self.runtime.tools.call(pid, 'delete_file', {'path': path})
        assert not denied.ok
        assert 'lacks delete' in (denied.error or '')
        assert 'does not exist' not in (denied.error or '')

    def test_human_output_tool_cannot_bypass_human_capability(self) -> None:
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='speak to the human')
        denied = self.runtime.tools.call(pid, 'human_output', {'message': 'denied'})
        assert not denied.ok
        assert self.human_output == []
        self.runtime.capability.grant(pid, 'human:owner', [CapabilityRight.WRITE], issued_by='test')
        allowed = self.runtime.tools.call(pid, 'human_output', {'message': 'allowed'})
        assert allowed.ok
        assert self.human_output == ['allowed']
        assert self.runtime.human.list(pid)[0].status == HumanRequestStatus.DELIVERED
        assert 'human.output' in self._audit_actions()

    def test_human_output_does_not_write_provider_when_effect_commit_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='human effect failure')
        cap = self.runtime.capability.grant_once(pid, 'human:owner', [CapabilityRight.WRITE], issued_by='test')

        def fail_insert_external_effect(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError('external effect unavailable')

        monkeypatch.setattr(self.runtime.store, 'insert_external_effect', fail_insert_external_effect)

        result = self.runtime.tools.call(pid, 'human_output', {'message': 'not visible'})

        assert not result.ok
        assert 'failed during execution' in (result.error or '')
        assert self.human_output == []
        assert self.runtime.human.list(pid)[0].status == HumanRequestStatus.PENDING
        assert self.runtime.store.get_capability(cap.cap_id).uses_remaining == 1
        assert 'human.output' not in self._audit_actions()

    def test_human_output_visible_write_is_not_left_pending_if_final_status_update_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='human final update failure')
        self.runtime.capability.grant(pid, 'human:owner', [CapabilityRight.WRITE], issued_by='test')
        original_update = self.runtime.store.update_human_request
        calls = 0

        def fail_second_update(request: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('late update failed')
            original_update(request)

        monkeypatch.setattr(self.runtime.store, 'update_human_request', fail_second_update)

        result = self.runtime.tools.call(pid, 'human_output', {'message': 'visible'})

        assert result.ok, result.error
        assert self.human_output == ['visible']
        assert self.runtime.human.list(pid)[0].status == HumanRequestStatus.DELIVERED
        assert 'human.output' in self._audit_actions()
        assert [item for item in self.runtime.store.list_external_effects() if item.provider == 'human']

    def test_human_output_preserves_non_terminal_channel_in_observability(self) -> None:
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='speak on gui channel')
        self.runtime.capability.grant(pid, 'human:owner', [CapabilityRight.WRITE], issued_by='test')

        allowed = self.runtime.tools.call(pid, 'human_output', {'message': 'allowed', 'channel': 'gui'})

        assert allowed.ok, allowed.error
        assert allowed.payload['channel'] == 'gui'
        assert self.human_output == ['allowed']
        request = self.runtime.human.list(pid)[0]
        assert request.status == HumanRequestStatus.DELIVERED
        assert request.payload['channel'] == 'gui'
        event = next(event for event in self.runtime.events.list(target='human:owner') if event.type == EventType.HUMAN_OUTPUT)
        assert event.payload['channel'] == 'gui'
        audit = next(record for record in self.runtime.audit.trace() if record.action == 'human.output')
        assert audit.decision['channel'] == 'gui'
        effect = self.runtime.store.list_external_effects()[0]
        assert effect.provider_metadata['context']['channel'] == 'gui'

    def test_human_output_rejects_empty_or_too_long_channel(self) -> None:
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='bad human channel')
        self.runtime.capability.grant(pid, 'human:owner', [CapabilityRight.WRITE], issued_by='test')

        empty = self.runtime.tools.call(pid, 'human_output', {'message': 'empty channel', 'channel': '   '})
        too_long = self.runtime.tools.call(pid, 'human_output', {'message': 'long channel', 'channel': 'x' * 129})

        assert not empty.ok
        assert 'human output channel must be non-empty' in (empty.error or '')
        assert not too_long.ok
        assert 'human output channel is too long' in (too_long.error or '')
        assert self.human_output == []
        assert self.runtime.human.list(pid) == []

    def test_one_time_human_output_capability_is_consumed_after_delivery(self) -> None:
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='speak once')
        self.runtime.capability.grant_once(pid, 'human:owner', [CapabilityRight.WRITE], issued_by='test')
        first = self.runtime.tools.call(pid, 'human_output', {'message': 'first'})
        second = self.runtime.tools.call(pid, 'human_output', {'message': 'second'})
        assert first.ok, first.error
        assert not second.ok
        assert self.human_output == ['first']
        assert not self.runtime.capability.check(pid, 'human:owner', CapabilityRight.WRITE)

    def test_process_cannot_call_tool_outside_creation_tool_table(self) -> None:
        pid = self.runtime.process.spawn(image='toolmaker-agent:v0', goal='call unavailable tool')
        denied = self.runtime.tools.call(pid, 'write_text_file', {'path': 'agent_outputs/no_tool.txt', 'content': 'x'})
        assert not denied.ok
        assert 'not in process tool table' in (denied.error or '')
        assert 'human.query' not in self._audit_actions()

    def test_workflow_run_cannot_bypass_image_tool_table(self) -> None:
        result = self.runtime.run_workflow('parse_pytest_log', {'log': 'FAILED tests/x.py::test_y'})

        assert not result.ok
        assert result.status == ProcessStatus.FAILED.value
        assert 'not in process tool table' in (result.error or '')
        process = self.runtime.process.get(result.pid)
        assert process.image_id == self.runtime.config.runtime.default_image_id
        assert process.status == ProcessStatus.FAILED
        assert 'workflow.run' in self._audit_actions()
        assert 'primitive.filesystem.read_text' not in self._audit_actions()

    def test_path_escape_is_denied_by_filesystem_primitive(self) -> None:
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='escape workspace')
        self.runtime.filesystem.grant_workspace(pid, [CapabilityRight.WRITE], issued_by='test')
        denied = self.runtime.tools.call(pid, 'write_text_file', {'path': '../outside.txt', 'content': 'denied'})
        assert not denied.ok
        assert 'escapes filesystem adapter root' in (denied.error or '')
        assert 'primitive.filesystem.write_text' not in self._audit_actions()

    def test_revoked_filesystem_capability_denies_write(self) -> None:
        path = f'agent_outputs/revoked_write_{uuid4().hex}.txt'
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='revoked write')
        cap = self.runtime.filesystem.grant_path(pid, path, [CapabilityRight.WRITE], issued_by='test')
        self.runtime.capability.revoke(cap.cap_id, revoked_by='test', reason='boundary test')
        denied = self.runtime.tools.call(pid, 'write_text_file', {'path': path, 'content': 'denied'})
        assert not denied.ok
        assert not (self.runtime.workspace_root / path).exists()
        assert 'primitive.filesystem.write_text' not in self._audit_actions()

    def test_fork_does_not_inherit_parent_filesystem_write_capability(self) -> None:
        path = f'agent_outputs/fork_write_{uuid4().hex}.txt'
        parent = self.runtime.process.spawn(image='review-agent:v0', goal='parent')
        self.runtime.filesystem.grant_path(parent, path, [CapabilityRight.WRITE], issued_by='test')
        child = self.runtime.process.fork(parent, goal='child', mode=ForkMode.WORKER)
        denied = self.runtime.tools.call(child, 'write_text_file', {'path': path, 'content': 'denied'})
        allowed = self.runtime.tools.call(parent, 'write_text_file', {'path': path, 'content': 'allowed'})
        assert not denied.ok
        assert allowed.ok
        assert (self.runtime.workspace_root / path).read_text(encoding='utf-8') == 'allowed'

    def _write_workspace_fixture(self, content: str) -> str:
        path = f'agent_outputs/boundary_read_{uuid4().hex}.txt'
        target = self.runtime.workspace_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        return path

    def _audit_actions(self) -> list[str]:
        return [record.action for record in self.runtime.audit.trace()]

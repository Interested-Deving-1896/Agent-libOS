from __future__ import annotations
import pytest
import contextlib
import io
import json
import tempfile
from pathlib import Path
from typing import Any
from agent_libos import Runtime
from agent_libos.api.cli import main as cli_main
from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import AgentLibOSConfig, CheckpointDefaults
from agent_libos.models import CapabilityEffect, CapabilityRight, ExternalEffectClassification, ExternalEffectRollbackClass, ExternalEffectRollbackStatus, HumanRequestStatus, ObjectMetadata, ObjectPatch, ObjectType, ProcessMessageStatus, ProcessStatus, ToolCandidateStatus
from agent_libos.models.exceptions import CapabilityDenied, ValidationError
from agent_libos.runtime.syscalls import LibOSSyscallSession
from agent_libos.substrate import CommandResult, LocalHumanProvider, LocalResourceProviderSubstrate

class TestCheckpointManager:

    def test_legacy_full_table_snapshot_restore_are_disabled(self) -> None:
        runtime = Runtime.open('local')
        try:
            with pytest.raises(RuntimeError, match='full-table SQLite snapshots are disabled'):
                runtime.store.snapshot_tables()
            with pytest.raises(RuntimeError, match='full-table SQLite restore is disabled'):
                runtime.store.restore_tables({})
        finally:
            runtime.close()

    def test_restore_recovers_process_subtree_objects_capabilities_and_cwd_only(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='root')
            child = runtime.spawn_child_process(pid, 'child')
            unrelated = runtime.process.spawn(image='base-agent:v0', goal='other')
            handle = runtime.memory.create_object(pid, ObjectType.SUMMARY, {'version': 1}, ObjectMetadata(title='state'), immutable=False, name='state')
            checkpoint_id = runtime.checkpoint.create(pid, 'before mutation', actor=pid)
            runtime.memory.update_object(pid, handle, ObjectPatch(payload={'version': 2}))
            runtime.memory.create_object(pid, ObjectType.SUMMARY, {'temp': True}, name='temp')
            runtime.process.set_working_directory(pid, 'src')
            runtime.process.signal(child, 'terminate', {'reason': 'bad branch'})
            runtime.capability.grant(pid, 'test:temporary', [CapabilityRight.READ], issued_by='test')
            other_handle = runtime.memory.create_object(unrelated, ObjectType.SUMMARY, {'keep': True}, name='other')
            result = runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)
            restored = runtime.memory.get_object_by_name(pid, 'state')
            assert restored.payload == {'version': 1}
            with pytest.raises(Exception):
                runtime.memory.get_object_by_name(pid, 'temp')
            assert runtime.process.get(pid).working_directory == '.'
            assert runtime.process.get(child).status == ProcessStatus.RUNNABLE
            assert not runtime.capability.check(pid, 'test:temporary', CapabilityRight.READ)
            assert runtime.memory.get_object(unrelated, other_handle).payload == {'keep': True}
            assert result['restored_pids'] == [pid, child]
        finally:
            runtime.close()

    def test_restore_preserves_append_only_history_and_reports_external_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='write external file')
                runtime.filesystem.grant_path(pid, 'out.txt', [CapabilityRight.WRITE], issued_by='test')
                checkpoint_id = runtime.checkpoint.create(pid, 'before external write', actor=pid)
                before_audit_count = len(runtime.audit.trace())
                runtime.filesystem.write_text(pid, 'out.txt', 'side effect')
                after_write_audit_count = len(runtime.audit.trace())
                result = runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)
                assert len(runtime.audit.trace()) >= after_write_audit_count + 1
                assert after_write_audit_count > before_audit_count
                assert (Path(temp_dir) / 'out.txt').exists()
                assert result['external_effects_since_checkpoint']
                assert result['restore_external_policy'] == 'report_only'
                assert result['external_effect_summary']['by_rollback_class']['rollbackable'] == 1
                assert result['external_effects_since_checkpoint'][0]['provider'] == 'filesystem'
                assert result['external_effects_since_checkpoint'][0]['rollback_class'] == 'rollbackable'
                assert 'checkpoint.restore' in [record.action for record in runtime.audit.trace()]
            finally:
                runtime.close()

    def test_restore_reports_provider_decided_external_effect_classes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            substrate = LocalResourceProviderSubstrate(temp_dir)
            substrate.shell = ClassifiedShellProvider()
            substrate.human = LocalHumanProvider(output_sink=lambda _message: None)
            runtime = Runtime.open('local', substrate=substrate)
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='external effect classes')
                runtime.filesystem.grant_path(pid, 'out.txt', [CapabilityRight.WRITE], issued_by='test')
                runtime.capability.grant(pid, 'shell:git', [CapabilityRight.EXECUTE], issued_by='test')
                runtime.capability.grant(pid, 'human:owner', [CapabilityRight.WRITE], issued_by='test')
                checkpoint_id = runtime.checkpoint.create(pid, 'before external effects', actor=pid)
                runtime.filesystem.write_text(pid, 'out.txt', 'side effect')
                runtime.shell.run(pid, ['git', 'status', '--short'])
                runtime.human.output(pid, 'visible once')
                result = runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)
                summary = result['external_effect_summary']['by_rollback_class']
                effects = result['external_effects_since_checkpoint']
                assert result['status'] == 'restored'
                assert result['restore_external_policy'] == 'report_only'
                assert summary['rollbackable'] == 1
                assert summary['irreversible'] == 1
                assert summary['no_rollback_required'] == 1
                assert {(effect['provider'], effect['rollback_class']) for effect in effects} == {('filesystem', 'rollbackable'), ('shell', 'irreversible'), ('human', 'no_rollback_required')}
            finally:
                runtime.close()

    def test_checkpoint_external_effect_report_is_scoped_to_process_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                owner = runtime.process.spawn(image='base-agent:v0', goal='owner')
                child = runtime.spawn_child_process(owner, 'child')
                unrelated = runtime.process.spawn(image='base-agent:v0', goal='other')
                runtime.filesystem.grant_path(owner, 'owner.txt', [CapabilityRight.WRITE], issued_by='test')
                runtime.filesystem.grant_path(child, 'child.txt', [CapabilityRight.WRITE], issued_by='test')
                runtime.filesystem.grant_path(unrelated, 'other.txt', [CapabilityRight.WRITE], issued_by='test')
                checkpoint_id = runtime.checkpoint.create(owner, 'before external writes', actor=owner)
                runtime.filesystem.write_text(owner, 'owner.txt', 'owner')
                runtime.filesystem.write_text(child, 'child.txt', 'child')
                runtime.filesystem.write_text(unrelated, 'other.txt', 'other')
                diff = runtime.checkpoint.diff(checkpoint_id, actor=owner)
                result = runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)
                assert diff['external_effect_summary']['total'] == 2
                assert result['external_effect_summary']['total'] == 2
                assert {effect['target'] for effect in result['external_effects_since_checkpoint']} == {'filesystem:workspace:owner.txt', 'filesystem:workspace:child.txt'}
            finally:
                runtime.close()

    def test_checkpoint_capabilities_gate_inspect_restore_and_fork(self) -> None:
        runtime = Runtime.open('local')
        try:
            owner = runtime.process.spawn(image='base-agent:v0', goal='owner')
            other = runtime.process.spawn(image='base-agent:v0', goal='other')
            checkpoint_id = runtime.checkpoint.create(owner, 'owned', actor=owner)
            assert runtime.checkpoint.inspect(checkpoint_id, actor=owner)['checkpoint']['pid'] == owner
            with pytest.raises(CapabilityDenied):
                runtime.checkpoint.inspect(checkpoint_id, actor=other)
            with pytest.raises(CapabilityDenied):
                runtime.checkpoint.restore(owner, checkpoint_id)
            with pytest.raises(CapabilityDenied):
                runtime.checkpoint.fork_from_checkpoint(owner, checkpoint_id)
            runtime.capability.grant(owner, f'checkpoint:{checkpoint_id}', [CapabilityRight.ADMIN], issued_by='test')
            assert runtime.checkpoint.restore(owner, checkpoint_id)['status'] == 'restored'
            runtime.capability.grant(owner, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            forked = runtime.checkpoint.fork_from_checkpoint(owner, checkpoint_id)
            assert forked['fork_root_pid'] != owner
        finally:
            runtime.close()

    def test_restore_supersedes_post_checkpoint_mailbox_and_cancels_human_requests(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='messages')
            checkpoint_id = runtime.checkpoint.create(pid, 'before messages', actor=pid)
            message = runtime.human.send_process_message(pid, 'late update')
            request_id = runtime.human.ask(pid, 'approve?', blocking=True)
            result = runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)
            assert runtime.store.get_process_message(message.message_id).status == ProcessMessageStatus.SUPERSEDED_BY_RESTORE
            assert runtime.messages.unread(pid) == []
            assert runtime.human.get(request_id).status == HumanRequestStatus.CANCELLED
            assert result['superseded_messages'] == [message.message_id]
            assert result['cancelled_human_requests'] == [request_id]
        finally:
            runtime.close()

    def test_fork_from_checkpoint_remaps_process_namespace_objects_and_capabilities(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='fork')
            original = runtime.memory.create_object(pid, ObjectType.SUMMARY, {'value': 7}, name='state')
            runtime.capability.grant(pid, 'filesystem:workspace:README.md', [CapabilityRight.READ], issued_by='test')
            checkpoint_id = runtime.checkpoint.create(pid, 'fork point', actor=pid)
            runtime.capability.grant(pid, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            forked = runtime.checkpoint.fork_from_checkpoint(pid, checkpoint_id)
            fork_pid = forked['fork_root_pid']
            fork_obj = runtime.memory.get_object_by_name(fork_pid, 'state')
            assert fork_pid != pid
            assert fork_obj.oid != original.oid
            assert fork_obj.namespace == runtime.memory.process_namespace(fork_pid)
            assert fork_obj.payload == {'value': 7}
            assert runtime.capability.check(fork_pid, 'filesystem:workspace:README.md', CapabilityRight.READ)
        finally:
            runtime.close()

    def test_fork_from_checkpoint_does_not_resurrect_revoked_capability(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='fork revoked capability')
            resource = runtime.filesystem.resource_for_path('secret.txt')
            cap = runtime.capability.grant(pid, resource, [CapabilityRight.READ], issued_by='test')
            checkpoint_id = runtime.checkpoint.create(pid, 'before revoke', actor=pid)
            runtime.capability.grant(pid, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            runtime.capability.revoke(cap.cap_id, revoked_by=pid, reason='holder gave up authority')
            forked = runtime.checkpoint.fork_from_checkpoint(pid, checkpoint_id)
            fork_root = forked['fork_root_pid']
            assert not runtime.capability.check(pid, resource, CapabilityRight.READ)
            assert not runtime.capability.check(fork_root, resource, CapabilityRight.READ)
            assert resource not in [capability.resource for capability in runtime.capability.list_subject(fork_root)]
        finally:
            runtime.close()

    def test_fork_from_checkpoint_respects_post_checkpoint_deny_policy(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='fork denied capability')
            secret = runtime.filesystem.resource_for_path('secret.txt')
            runtime.capability.grant(pid, 'filesystem:workspace:*', [CapabilityRight.READ], issued_by='test')
            checkpoint_id = runtime.checkpoint.create(pid, 'before deny policy', actor=pid)
            runtime.capability.grant(pid, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            runtime.capability.issue_trusted(pid, secret, [CapabilityRight.READ], issued_by='test', effect=CapabilityEffect.DENY)
            forked = runtime.checkpoint.fork_from_checkpoint(pid, checkpoint_id)
            fork_root = forked['fork_root_pid']
            assert not runtime.capability.check(pid, secret, CapabilityRight.READ)
            assert not runtime.capability.check(fork_root, secret, CapabilityRight.READ)
            assert not runtime.capability.check(fork_root, 'filesystem:workspace:public.txt', CapabilityRight.READ)
        finally:
            runtime.close()

    def test_fork_from_checkpoint_normalizes_waiting_process_state(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='base-agent:v0', goal='waiting parent')
            child = runtime.spawn_child_process(parent, 'unfinished child')
            with pytest.raises(TimeoutError):
                runtime.process.wait(parent, child, timeout=0)
            assert runtime.process.get(parent).status == ProcessStatus.WAITING_EVENT
            checkpoint_id = runtime.checkpoint.create(parent, 'waiting fork point', actor=parent)
            runtime.capability.grant(parent, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            forked = runtime.checkpoint.fork_from_checkpoint(parent, checkpoint_id)
            fork_root = runtime.process.get(forked['fork_root_pid'])
            assert fork_root.status == ProcessStatus.RUNNABLE
            assert fork_root.status_message is None
        finally:
            runtime.close()

    def test_checkpoint_fork_parent_attachment_requires_authority(self) -> None:
        runtime = Runtime.open('local')
        try:
            owner = runtime.process.spawn(image='base-agent:v0', goal='owner')
            other = runtime.process.spawn(image='base-agent:v0', goal='other')
            checkpoint_id = runtime.checkpoint.create(owner, 'fork parent boundary', actor=owner)
            runtime.capability.grant(owner, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            with pytest.raises(CapabilityDenied):
                runtime.checkpoint.fork_from_checkpoint(owner, checkpoint_id, parent_pid=other)
            runtime.capability.grant(owner, runtime.checkpoint.process_resource(other), [CapabilityRight.ADMIN], issued_by='test')
            forked = runtime.checkpoint.fork_from_checkpoint(owner, checkpoint_id, parent_pid=other)
            assert runtime.process.get(forked['fork_root_pid']).parent_pid == other
        finally:
            runtime.close()

    def test_checkpoint_restores_registered_jit_tool_source(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='toolmaker-agent:v0', goal='jit checkpoint')
            source = 'export function run(args, libos) { return { value: args.value }; }'
            candidate_id = runtime.tools.propose(pid, {'name': 'echo_value', 'description': 'Echo a value.', 'input_schema': {'type': 'object', 'properties': {'value': {'type': 'string'}}}, 'output_schema': {'type': 'object'}}, source_code=source)
            candidate = runtime.store.get_tool_candidate(candidate_id)
            candidate.status = ToolCandidateStatus.VALIDATED
            candidate.validation = {'ok': True, 'language': 'typescript'}
            runtime.store.update_tool_candidate(candidate)
            handle = runtime.tools.register(pid, candidate_id)
            checkpoint_id = runtime.checkpoint.create(pid, 'jit registered', actor=pid)
            runtime.tools._jit_sources.pop(handle.tool_id)
            runtime.tools._handles.pop(handle.tool_id)
            runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)
            assert runtime.tools._jit_sources[handle.tool_id] == source
            assert runtime.tools.resolve('echo_value', pid=pid).tool_id == handle.tool_id
            runtime.tools._jit_sources.pop(handle.tool_id)
            runtime.tools._handles.pop(handle.tool_id)
            runtime.capability.grant(pid, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            forked = runtime.checkpoint.fork_from_checkpoint(pid, checkpoint_id)
            assert runtime.tools._jit_sources[handle.tool_id] == source
            assert runtime.tools.resolve('echo_value', pid=forked['fork_root_pid']).tool_id == handle.tool_id
        finally:
            runtime.close()

    def test_checkpoint_payload_capture_limit_is_enforced(self) -> None:
        config = AgentLibOSConfig(checkpoint=CheckpointDefaults(payload_capture_limit_bytes=8))
        runtime = Runtime.open('local', config=config)
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='large payload')
            runtime.memory.create_object(pid, ObjectType.SUMMARY, {'data': 'too large'}, name='large')
            with pytest.raises(ValidationError):
                runtime.checkpoint.create(pid, 'too large', actor=pid)
        finally:
            runtime.close()

    def test_checkpoint_syscalls_use_primitive_capabilities(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='syscall')
            other = runtime.process.spawn(image='base-agent:v0', goal='other')
            session = LibOSSyscallSession(runtime, pid)
            other_session = LibOSSyscallSession(runtime, other)
            checkpoint = self._run(session.handle('checkpoint.create', {'reason': 'syscall'}))
            inspected = self._run(session.handle('checkpoint.inspect', {'checkpoint_id': checkpoint['checkpoint_id']}))
            assert inspected['checkpoint']['pid'] == pid
            with pytest.raises(CapabilityDenied):
                self._run(other_session.handle('checkpoint.inspect', {'checkpoint_id': checkpoint['checkpoint_id']}))
        finally:
            runtime.close()

    def test_checkpoint_safe_point_normalizes_running_status(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='safe point')
            process = runtime.process.get(pid)
            process.status = ProcessStatus.RUNNING
            runtime.store.update_process(process)
            checkpoint_id = runtime.checkpoint.create(pid, 'running safe point', actor=pid)
            inspected = runtime.checkpoint.inspect(checkpoint_id, actor=pid)
            runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)
            assert inspected['processes'][0]['status'] == ProcessStatus.RUNNABLE.value
            assert runtime.process.get(pid).status == ProcessStatus.RUNNABLE
        finally:
            runtime.close()

    def test_default_images_expose_only_low_risk_checkpoint_tools(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='tool table')
            assert runtime.tools.call(pid, 'create_checkpoint', {'reason': 'tool'}).ok
            assert 'create_checkpoint' in runtime.process.get(pid).tool_table
            assert 'inspect_checkpoint' in runtime.process.get(pid).tool_table
            assert 'diff_checkpoint' in runtime.process.get(pid).tool_table
            assert 'list_checkpoints' in runtime.process.get(pid).tool_table
            assert 'restore_checkpoint' not in runtime.process.get(pid).tool_table
            assert 'fork_checkpoint' not in runtime.process.get(pid).tool_table
        finally:
            runtime.close()

    def test_checkpoint_cli_outputs_stable_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / 'runtime.sqlite')
            spawned = self._cli_json(['--db', db_path, 'spawn', '--goal', 'cli checkpoint'])
            created = self._cli_json(['--db', db_path, 'checkpoint', 'create', spawned['pid'], 'cli reason'])
            listed = self._cli_json(['--db', db_path, 'checkpoint', 'list', '--pid', spawned['pid']])
            inspected = self._cli_json(['--db', db_path, 'checkpoint', 'inspect', created['checkpoint_id']])
            assert created['checkpoint_id'].startswith('ckpt_')
            assert listed[0]['checkpoint_id'] == created['checkpoint_id']
            assert inspected['checkpoint']['pid'] == spawned['pid']

    def _cli_json(self, argv: list[str]):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli_main(argv)
        return json.loads(buffer.getvalue())

    def _run(self, awaitable):
        import asyncio
        return asyncio.run(awaitable)

class ClassifiedShellProvider:

    def run(self, argv: list[str], *, timeout: float=30.0, cwd: str | None=None) -> CommandResult:
        return CommandResult(argv=list(argv), returncode=0, stdout='ok\n', stderr='')

    def classify_external_effect(self, operation: str, context: dict[str, Any], result: Any) -> ExternalEffectClassification:
        return ExternalEffectClassification(rollback_class=ExternalEffectRollbackClass.IRREVERSIBLE, rollback_status=ExternalEffectRollbackStatus.NOT_SUPPORTED, state_mutation=True, information_flow=True, metadata={'operation': operation})

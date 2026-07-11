from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from agent_libos import AgentImage, Runtime
from agent_libos.config import AgentLibOSConfig, CheckpointDefaults
from agent_libos.models import CapabilityEffect, CapabilityRight, EventType, HumanRequestStatus, ObjectMetadata, ObjectPatch, ObjectType, ProcessMessageStatus, ProcessStatus
from agent_libos.models.exceptions import CapabilityDenied, NotFound, ValidationError
from agent_libos.substrate import LocalHumanProvider, LocalResourceProviderSubstrate
from agent_libos.utils.serde import loads
from tests.support.checkpoints import ClassifiedShellProvider


class TestCheckpointRestore:
    def test_checkpoint_create_audit_failure_rolls_back_row_head_capability_and_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='atomic checkpoint creation')
            before_events = list(runtime.events.list())
            before_audit = list(runtime.audit.trace())
            before_checkpoint_capabilities = {
                capability.cap_id
                for capability in runtime.capability.capabilities_for(pid)
                if capability.resource.startswith('checkpoint:')
            }
            original_record = runtime.audit.record

            def fail_create_audit(*args: object, **kwargs: object) -> object:
                if kwargs.get('action') == 'checkpoint.create':
                    raise RuntimeError('injected checkpoint create audit failure')
                return original_record(*args, **kwargs)

            monkeypatch.setattr(runtime.audit, 'record', fail_create_audit)
            with pytest.raises(RuntimeError, match='injected checkpoint create audit failure'):
                runtime.checkpoint.create(pid, 'must be atomic', actor=pid, require_capability=False)

            assert runtime.process.get(pid).checkpoint_head is None
            assert runtime.checkpoint.list(pid, actor=pid, require_capability=False) == []
            assert runtime.events.list() == before_events
            assert runtime.audit.trace() == before_audit
            assert {
                capability.cap_id
                for capability in runtime.capability.capabilities_for(pid)
                if capability.resource.startswith('checkpoint:')
            } == before_checkpoint_capabilities
        finally:
            runtime.close()


    def test_restore_advances_llm_context_generation_to_break_provider_chain(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='restore breaks provider chain')
            runtime.store.set_llm_context_generation(pid, 'generation-before-checkpoint')
            checkpoint_id = runtime.checkpoint.create(pid, 'provider chain boundary', actor=pid)
            runtime.store.set_llm_context_generation(pid, 'generation-after-checkpoint')

            runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            generation = runtime.store.get_llm_context_generation(pid)
            assert generation not in {'generation-before-checkpoint', 'generation-after-checkpoint'}
        finally:
            runtime.close()

    def test_restore_reconciles_plural_human_wait_state_after_all_requests_resolve(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='plural human wait restore')
            first = runtime.human.query(
                pid,
                'owner',
                {'type': 'question', 'question': 'First?'},
                blocking=True,
            )
            second = runtime.human.query(
                pid,
                'owner',
                {'type': 'question', 'question': 'Second?'},
                blocking=True,
            )
            runtime.human.approve(first, {'approved': True, 'answer': 'one'})
            waiting = runtime.process.get(pid)
            assert waiting.status == ProcessStatus.WAITING_HUMAN
            assert waiting.status_message == f'waiting for human requests {second}'
            checkpoint_id = runtime.checkpoint.create(pid, 'plural human wait', actor=pid)
            runtime.human.approve(second, {'approved': True, 'answer': 'two'})
            assert runtime.process.get(pid).status == ProcessStatus.RUNNABLE

            runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            restored = runtime.process.get(pid)
            assert restored.status == ProcessStatus.RUNNABLE
            assert restored.status_message is None
        finally:
            runtime.close()

    def test_legacy_full_table_snapshot_restore_are_disabled(self) -> None:
        runtime = Runtime.open('local')
        try:
            with pytest.raises(RuntimeError, match='full-table SQLite snapshots are disabled'):
                runtime.store.snapshot_tables()
            with pytest.raises(RuntimeError, match='full-table SQLite restore is disabled'):
                runtime.store.restore_tables({})
        finally:
            runtime.close()

    def test_restore_invalidates_inflight_capability_reservations_before_replacing_scope(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='restore reservation boundary')
            cap = runtime.capability.issue_trusted(
                pid,
                'object:restore-reservation',
                [CapabilityRight.READ],
                issued_by='test',
                uses_remaining=2,
            )
            checkpoint_id = runtime.checkpoint.create(pid, 'before reservation', actor=pid)
            decision = runtime.capability.authorize(pid, cap.resource, CapabilityRight.READ)
            reservation_id = runtime.capability.reserve_decision_use(
                decision,
                used_by='test',
                reason='effect in flight while checkpoint restore begins',
            )
            assert reservation_id is not None
            assert runtime.store.get_capability(cap.cap_id).uses_remaining == 1

            runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            restored = runtime.store.get_capability(cap.cap_id)
            assert restored is not None
            assert restored.uses_remaining == 1
            assert runtime.capability._restore_reserved_use(
                reservation_id,
                restored_by='test',
                reason='late provider cleanup after scope replacement',
            ) is None
            assert runtime.store.get_capability(cap.cap_id).uses_remaining == 1
        finally:
            runtime.close()

    def test_restore_recovers_process_subtree_objects_capabilities_and_cwd_only(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='root')
            runtime.capability.grant(pid, 'process:spawn', [CapabilityRight.WRITE], issued_by='test')
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

    def test_restore_does_not_resurrect_revoked_or_currently_denied_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='restore authority')
                (Path(temp_dir) / 'secret.txt').write_text('secret', encoding='utf-8')
                secret = runtime.filesystem.resource_for_path('secret.txt')
                cap = runtime.filesystem.grant_path(pid, 'secret.txt', [CapabilityRight.READ], issued_by='test')
                checkpoint_id = runtime.checkpoint.create(pid, 'before revoke', actor=pid)
                runtime.capability.revoke(cap.cap_id, revoked_by=pid, reason='revoked before restore')
                runtime.capability.issue_trusted(pid, secret, [CapabilityRight.READ], issued_by='test', effect=CapabilityEffect.DENY)

                runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

                assert not runtime.capability.check(pid, secret, CapabilityRight.READ)
                with pytest.raises(CapabilityDenied):
                    runtime.filesystem.read_text(pid, 'secret.txt')
            finally:
                runtime.close()

    def test_restore_concurrent_revoke_wins_over_snapshot_capability_reinsertion(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open('local')
        filter_reached = threading.Event()
        revoke_started = threading.Event()
        revoke_done = threading.Event()
        restore_errors: list[BaseException] = []
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='concurrent restore revoke')
            cap = runtime.capability.grant(pid, 'test:restore-race', [CapabilityRight.READ], issued_by='test')
            checkpoint_id = runtime.checkpoint.create(pid, 'before concurrent revoke', actor=pid)
            original_filter = runtime.checkpoint._filtered_restored_capability_rows

            def pause_after_filter(rows):
                filtered = original_filter(rows)
                filter_reached.set()
                assert revoke_started.wait(timeout=2)
                return filtered

            monkeypatch.setattr(runtime.checkpoint, '_filtered_restored_capability_rows', pause_after_filter)

            def restore() -> None:
                try:
                    runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)
                except BaseException as exc:  # pragma: no cover - asserted below
                    restore_errors.append(exc)

            restore_thread = threading.Thread(target=restore)
            restore_thread.start()
            assert filter_reached.wait(timeout=2)

            def revoke() -> None:
                revoke_started.set()
                runtime.capability.revoke(cap.cap_id, revoked_by=pid, reason='concurrent revoke wins')
                revoke_done.set()

            revoke_thread = threading.Thread(target=revoke)
            revoke_thread.start()
            restore_thread.join(timeout=2)
            revoke_thread.join(timeout=2)

            assert not restore_thread.is_alive()
            assert not revoke_thread.is_alive()
            assert revoke_done.is_set()
            assert restore_errors == []
            restored = runtime.store.get_capability(cap.cap_id)
            assert restored is not None
            assert not restored.active
            assert not runtime.capability.check(pid, cap.resource, CapabilityRight.READ)
        finally:
            runtime.close()

    @pytest.mark.parametrize(
        'mutation_kind',
        ['spawn', 'capability', 'object', 'message', 'object_task'],
    )
    def test_restore_serializes_host_mutations_across_preflight_and_commit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mutation_kind: str,
    ) -> None:
        runtime = Runtime.open('local')
        preflight_reached = threading.Event()
        mutation_started = threading.Event()
        allow_commit = threading.Event()
        main_state_committed = threading.Event()
        mutation_finished = threading.Event()
        mutation_before_commit: list[bool] = []
        errors: list[BaseException] = []
        mutation_results: list[object] = []
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='restore host mutation boundary')
            owner = None
            if mutation_kind in {'spawn', 'object_task'}:
                runtime.capability.grant(pid, 'process:spawn', [CapabilityRight.WRITE], issued_by='test')
            if mutation_kind == 'object_task':
                owner = runtime.memory.create_object(
                    pid,
                    ObjectType.ARTIFACT,
                    {'owner': True},
                    name='restore.mutation.owner',
                    immutable=False,
                )
            checkpoint_id = runtime.checkpoint.create(pid, 'before host mutation race', actor=pid)
            original_validate = runtime.checkpoint._validate_snapshot_restore_assets
            original_restore_rows = runtime.checkpoint._restore_scoped_rows

            def pause_preflight(snapshot):
                original_validate(snapshot)
                preflight_reached.set()
                assert mutation_started.wait(timeout=2)
                assert allow_commit.wait(timeout=2)

            def mark_main_commit(*args, **kwargs):
                result = original_restore_rows(*args, **kwargs)
                main_state_committed.set()
                return result

            monkeypatch.setattr(runtime.checkpoint, '_validate_snapshot_restore_assets', pause_preflight)
            monkeypatch.setattr(runtime.checkpoint, '_restore_scoped_rows', mark_main_commit)

            def restore() -> None:
                try:
                    runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            restore_thread = threading.Thread(target=restore)
            restore_thread.start()
            assert preflight_reached.wait(timeout=2)

            def mutate() -> None:
                try:
                    mutation_started.set()
                    if mutation_kind == 'spawn':
                        mutation_results.append(runtime.process.spawn_child(pid, 'spawn racing restore'))
                    elif mutation_kind == 'capability':
                        mutation_results.append(
                            runtime.capability.grant(
                                pid,
                                'test:capability-racing-restore',
                                [CapabilityRight.READ],
                                issued_by='test',
                            )
                        )
                    elif mutation_kind == 'object':
                        mutation_results.append(
                            runtime.memory.create_object(
                                pid,
                                ObjectType.SUMMARY,
                                {'racing': True},
                                name='restore.mutation.object',
                            )
                        )
                    elif mutation_kind == 'message':
                        mutation_results.append(
                            runtime.messages.post(
                                sender='test',
                                recipient_pid=pid,
                                subject='message racing restore',
                            )
                        )
                    else:
                        assert owner is not None
                        mutation_results.append(
                            runtime.object_tasks.start(
                                pid,
                                owner,
                                'receive_process_messages',
                                {'channel': 'restore-mutation-never'},
                            )
                        )
                    mutation_before_commit.append(not main_state_committed.is_set())
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)
                finally:
                    mutation_finished.set()

            mutation_thread = threading.Thread(target=mutate)
            mutation_thread.start()
            assert mutation_started.wait(timeout=2)
            mutation_finished.wait(timeout=0.2)
            allow_commit.set()
            restore_thread.join(timeout=3)
            mutation_thread.join(timeout=3)

            assert not restore_thread.is_alive()
            assert not mutation_thread.is_alive()
            assert errors == []
            assert mutation_before_commit == [False]
            assert len(mutation_results) == 1
        finally:
            runtime.close()

    def test_restore_does_not_roll_back_borrowed_view_root_or_external_capabilities(self) -> None:
        runtime = Runtime.open('local')
        try:
            owner = runtime.process.spawn(image='base-agent:v0', goal='shared object owner')
            borrower = runtime.process.spawn(image='base-agent:v0', goal='checkpoint borrower')
            shared = runtime.memory.create_object(
                owner,
                ObjectType.SUMMARY,
                {'version': 1},
                ObjectMetadata(title='shared'),
                immutable=False,
                name='shared.state',
            )
            borrowed = runtime.capability.handle_for_object(
                borrower,
                shared.oid,
                [CapabilityRight.READ],
                issued_by='test.borrowed',
            )
            runtime._add_handle_to_process_view(borrower, borrowed)
            checkpoint_id = runtime.checkpoint.create(borrower, 'borrowed root checkpoint', actor=borrower)

            runtime.memory.update_object(owner, shared, ObjectPatch(payload={'version': 2}))
            owner_capability = runtime.store.get_capability(shared.capability_id)
            assert owner_capability is not None and owner_capability.active

            runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            assert runtime.store.get_object(shared.oid).payload == {'version': 2}
            owner_capability = runtime.store.get_capability(shared.capability_id)
            assert owner_capability is not None and owner_capability.active
            assert runtime.capability.check(owner, f'object:{shared.oid}', CapabilityRight.WRITE)
            restored_borrower = runtime.process.get(borrower)
            assert shared.oid in {handle.oid for handle in restored_borrower.memory_view.roots}
        finally:
            runtime.close()

    def test_replay_to_event_is_scoped_to_checkpoint_process_subtree(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='replay scoped')
            unrelated = runtime.process.spawn(image='base-agent:v0', goal='unrelated')
            checkpoint_id = runtime.checkpoint.create(pid, 'before events', actor=pid)
            unrelated_event = runtime.events.emit(
                EventType.EXTERNAL_WRITE,
                source=unrelated,
                target=unrelated,
                payload={'secret': 'unrelated'},
            )
            related_event = runtime.events.emit(EventType.PROCESS_SIGNAL, source=pid, target=pid, payload={'ok': True})

            with pytest.raises(NotFound):
                runtime.checkpoint.replay_to_event(checkpoint_id, unrelated_event.event_id, actor=pid)
            replayed = runtime.checkpoint.replay_to_event(checkpoint_id, related_event.event_id, actor=pid)

            replayed_ids = [event['event_id'] for event in replayed['events']]
            assert unrelated_event.event_id not in replayed_ids
            assert replayed_ids[-1] == related_event.event_id
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
                runtime.capability.grant(owner, 'process:spawn', [CapabilityRight.WRITE], issued_by='test')
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

    def test_restore_supersedes_post_checkpoint_mailbox_and_cancels_human_requests(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(
                image='base-agent:v0',
                goal='messages',
                authority_manifest={
                    'authorized_capabilities': [
                        {'resource': 'human:owner', 'rights': [CapabilityRight.WRITE.value]}
                    ]
                },
            )
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

    def test_checkpoint_create_captures_object_row_and_payload_from_one_store_snapshot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open('local')
        snapshot_reached_payloads = threading.Event()
        writer_attempted = threading.Event()
        writer_acquired_store = threading.Event()
        allow_writer_commit = threading.Event()
        writer_errors: list[BaseException] = []
        writer: threading.Thread | None = None
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='consistent checkpoint')
            handle = runtime.memory.create_object(
                pid,
                ObjectType.SUMMARY,
                {'generation': 1},
                ObjectMetadata(title='consistent state'),
                immutable=False,
                name='consistent.state',
            )
            before = runtime.store.get_object(handle.oid)
            assert before is not None
            original_payload_snapshot = runtime.checkpoint._object_payload_snapshot

            def coordinated_payload_snapshot(object_oids: list[str]) -> dict[str, object]:
                snapshot_reached_payloads.set()
                assert writer_attempted.wait(timeout=5)
                # Without a transaction spanning the checkpoint reads, the
                # writer acquires the store here and can split the object row
                # from its payload. With the transaction it remains blocked
                # until the checkpoint has been durably inserted.
                writer_acquired_store.wait(timeout=0.2)
                allow_writer_commit.set()
                return original_payload_snapshot(object_oids)

            monkeypatch.setattr(runtime.checkpoint, '_object_payload_snapshot', coordinated_payload_snapshot)

            def mutate_row_and_payload_atomically() -> None:
                try:
                    assert snapshot_reached_payloads.wait(timeout=5)
                    writer_attempted.set()
                    with runtime.store.transaction(include_object_payloads=True) as cur:
                        writer_acquired_store.set()
                        cur.execute(
                            'UPDATE objects SET version = ? WHERE oid = ?',
                            (before.version + 1, handle.oid),
                        )
                        runtime.store.set_object_payload(handle.oid, {'generation': 2})
                        assert allow_writer_commit.wait(timeout=5)
                except BaseException as exc:
                    writer_errors.append(exc)

            writer = threading.Thread(target=mutate_row_and_payload_atomically)
            writer.start()
            checkpoint_id = runtime.checkpoint.create(pid, 'consistent snapshot', actor=pid)
            writer.join(timeout=5)
            assert not writer.is_alive()
            assert writer_errors == []

            found = runtime.store.get_checkpoint_snapshot(checkpoint_id)
            assert found is not None
            _checkpoint, snapshot = found
            object_row = next(row for row in snapshot['rows']['objects'] if row['oid'] == handle.oid)
            captured_pair = (object_row['version'], snapshot['object_payloads'][handle.oid]['generation'])
            assert captured_pair in {(before.version, 1), (before.version + 1, 2)}
        finally:
            allow_writer_commit.set()
            if writer is not None:
                writer.join(timeout=5)
            runtime.close()

    def test_checkpoint_snapshot_canonicalizes_process_capability_index_during_concurrent_grant(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open('local')
        capability_inserted = threading.Event()
        allow_process_attach = threading.Event()
        grant_errors: list[BaseException] = []
        grant_result: list[object] = []
        checkpoint_started = threading.Event()
        checkpoint_done = threading.Event()
        checkpoint_errors: list[BaseException] = []
        checkpoint_result: list[str] = []
        grant_thread: threading.Thread | None = None
        checkpoint_thread: threading.Thread | None = None
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='capability snapshot')
            original_attach = runtime.capability._attach_to_process

            def pause_grant_attach(subject: str, cap_id: str) -> None:
                if threading.current_thread() is grant_thread:
                    capability_inserted.set()
                    assert allow_process_attach.wait(timeout=5)
                original_attach(subject, cap_id)

            monkeypatch.setattr(runtime.capability, '_attach_to_process', pause_grant_attach)

            def grant_capability() -> None:
                try:
                    grant_result.append(
                        runtime.capability.grant(pid, 'test:concurrent-checkpoint', [CapabilityRight.READ], issued_by='test')
                    )
                except BaseException as exc:
                    grant_errors.append(exc)

            grant_thread = threading.Thread(target=grant_capability)
            grant_thread.start()
            assert capability_inserted.wait(timeout=5)

            def create_checkpoint() -> None:
                checkpoint_started.set()
                try:
                    checkpoint_result.append(
                        runtime.checkpoint.create(pid, 'capability index snapshot', actor=pid)
                    )
                except BaseException as exc:
                    checkpoint_errors.append(exc)
                finally:
                    checkpoint_done.set()

            checkpoint_thread = threading.Thread(target=create_checkpoint)
            checkpoint_thread.start()
            assert checkpoint_started.wait(timeout=5)
            # Capability issue is now a single transaction.  A checkpoint must
            # wait instead of observing the inserted capability before its
            # process index attachment and evidence are committed.
            assert not checkpoint_done.wait(timeout=0.1)
            allow_process_attach.set()
            grant_thread.join(timeout=5)
            assert not grant_thread.is_alive()
            assert grant_errors == []
            assert len(grant_result) == 1
            checkpoint_thread.join(timeout=5)
            assert not checkpoint_thread.is_alive()
            assert checkpoint_errors == []
            assert len(checkpoint_result) == 1

            checkpoint_id = checkpoint_result[0]
            found = runtime.store.get_checkpoint_snapshot(checkpoint_id)
            assert found is not None
            _checkpoint, snapshot = found
            captured_capability = next(
                row
                for row in snapshot['rows']['capabilities']
                if row['resource'] == 'test:concurrent-checkpoint'
            )
            process_row = next(row for row in snapshot['rows']['processes'] if row['pid'] == pid)
            assert captured_capability['cap_id'] in loads(process_row['capabilities_json'], [])
        finally:
            allow_process_attach.set()
            if grant_thread is not None:
                grant_thread.join(timeout=5)
            if checkpoint_thread is not None:
                checkpoint_thread.join(timeout=5)
            runtime.close()

    def test_restore_refuses_while_scheduler_quantum_is_active(self) -> None:
        runtime = Runtime.open('local')
        resume = threading.Event()
        entered = threading.Event()
        errors: list[BaseException] = []
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='active restore')
            checkpoint_id = runtime.checkpoint.create(pid, 'before active quantum', actor=pid)

            def quantum(selected_pid: str) -> dict[str, object]:
                entered.set()
                assert selected_pid == pid
                assert resume.wait(timeout=5)
                return {'ok': True}

            def run_scheduler() -> None:
                try:
                    runtime.scheduler.run_pid_until_idle(pid, quantum, max_quanta=1)
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run_scheduler)
            thread.start()
            assert entered.wait(timeout=5)

            with pytest.raises(ValidationError, match='scheduler is running'):
                runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            resume.set()
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert errors == []
        finally:
            resume.set()
            runtime.close()

    def test_restore_runs_release_finalizers_for_deleted_scoped_objects(self) -> None:
        runtime = Runtime.open('local')
        calls: list[tuple[str, str, str]] = []
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='restore finalizer')
            checkpoint_id = runtime.checkpoint.create(pid, 'before temporary object', actor=pid)
            handle = runtime.memory.create_object(
                pid,
                ObjectType.SUMMARY,
                {'temp': True},
                ObjectMetadata(title='temp'),
                immutable=False,
                name='temp',
            )
            runtime.memory.bind_object_release_finalizer(
                lambda obj, actor, reason: calls.append((obj.oid, actor, reason))
            )

            runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            assert (handle.oid, 'checkpoint.restore', 'checkpoint_restore') in calls
            with pytest.raises(Exception):
                runtime.memory.get_object_by_name(pid, 'temp')
        finally:
            runtime.close()

    def test_restore_does_not_release_finalizers_for_restored_live_objects(self) -> None:
        runtime = Runtime.open('local')
        calls: list[tuple[str, str, str]] = []
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='restore keeps object')
            handle = runtime.memory.create_object(
                pid,
                ObjectType.SUMMARY,
                {'keep': True},
                ObjectMetadata(title='keep'),
                immutable=False,
                name='keep',
            )
            checkpoint_id = runtime.checkpoint.create(pid, 'after persistent object', actor=pid)
            runtime.memory.bind_object_release_finalizer(
                lambda obj, actor, reason: calls.append((obj.oid, actor, reason))
            )

            runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            assert calls == []
            assert runtime.store.get_object(handle.oid) is not None
            restored = runtime.memory.get_object_by_name(pid, 'keep')
            assert restored.oid == handle.oid
        finally:
            runtime.close()

    def test_restore_reconciles_terminal_human_wait_state(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(
                image='base-agent:v0',
                goal='human wait',
                authority_manifest={
                    'authorized_capabilities': [
                        {'resource': 'human:owner', 'rights': [CapabilityRight.WRITE.value]}
                    ]
                },
            )
            request_id = runtime.human.ask(pid, 'continue?', blocking=True)
            checkpoint_id = runtime.checkpoint.create(pid, 'waiting for human', actor=pid)
            runtime.human.approve(request_id, {'approved': True, 'answer': 'yes'})

            runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            restored = runtime.process.get(pid)
            assert restored.status == ProcessStatus.RUNNABLE
            assert restored.status_message is None
            assert runtime.human.get(request_id).status == HumanRequestStatus.APPROVED
        finally:
            runtime.close()

    def test_restore_refuses_scoped_active_object_task(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='object task restore')
            runtime.capability.grant(pid, 'process:spawn', [CapabilityRight.WRITE], issued_by='test')
            owner = runtime.memory.create_object(
                pid,
                ObjectType.ARTIFACT,
                {'name': 'owner'},
                metadata=ObjectMetadata(title='owner'),
                immutable=False,
            )
            checkpoint_id = runtime.checkpoint.create(pid, 'before task', actor=pid)
            task = runtime.object_tasks.start(pid, owner, 'receive_process_messages', {'channel': 'never'})
            waiting = runtime.object_tasks.wait(task.task_id, actor_pid=pid, timeout=2)

            with pytest.raises(ValidationError, match='ObjectTasks are active'):
                runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            assert runtime.object_tasks.get(task.task_id, actor_pid=pid).status == waiting.status
        finally:
            runtime.close()

    def test_restore_does_not_replace_current_image_without_image_write(self) -> None:
        runtime = Runtime.open('local')
        image_id = 'checkpoint-restore-image:v0'
        try:
            runtime.register_image(
                AgentImage(image_id=image_id, name='checkpoint-restore-image', system_prompt='snapshot prompt'),
                actor='test',
            )
            pid = runtime.process.spawn(image=image_id, goal='checkpoint image source')
            checkpoint_id = runtime.checkpoint.create(pid, 'image restore point', actor=pid)
            runtime.capability.grant(pid, f'checkpoint:{checkpoint_id}', [CapabilityRight.ADMIN], issued_by='test')
            runtime.register_image(
                AgentImage(image_id=image_id, name='checkpoint-restore-image', system_prompt='current prompt'),
                actor='test',
                replace=True,
            )

            with pytest.raises(CapabilityDenied, match=f'image:{image_id}'):
                runtime.checkpoint.restore(pid, checkpoint_id)

            assert runtime.get_image(image_id).system_prompt == 'current prompt'
            runtime.capability.grant(pid, runtime.image_registry.resource_for(image_id), [CapabilityRight.WRITE], issued_by='test')
            restored = runtime.checkpoint.restore(pid, checkpoint_id)
            assert restored['status'] == 'restored'
            assert runtime.get_image(image_id).system_prompt == 'snapshot prompt'
        finally:
            runtime.close()

    def test_restore_rolls_back_rows_and_payloads_when_insert_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime = Runtime.open('local')
        finalizer_calls: list[tuple[str, str, str]] = []
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='rollback restore')
            handle = runtime.memory.create_object(
                pid,
                ObjectType.SUMMARY,
                {'version': 1},
                ObjectMetadata(title='state'),
                immutable=False,
                name='state',
            )
            checkpoint_id = runtime.checkpoint.create(pid, 'before failed restore', actor=pid)
            runtime.memory.update_object(pid, handle, ObjectPatch(payload={'version': 2}))
            temporary = runtime.memory.create_object(
                pid,
                ObjectType.SUMMARY,
                {'temporary': True},
                ObjectMetadata(title='temporary'),
                immutable=False,
                name='temporary.after.checkpoint',
            )
            runtime.memory.bind_object_release_finalizer(
                lambda obj, actor, reason: finalizer_calls.append((obj.oid, actor, reason))
            )
            message = runtime.human.send_process_message(pid, 'late message')
            request_id = runtime.human.query(
                pid=pid,
                human=runtime.config.runtime.default_human,
                request={'type': 'question', 'question': 'still pending?'},
                blocking=False,
            )
            original_insert = runtime.checkpoint._insert_row

            def fail_on_process_insert(cur, table, row):
                if table == 'processes':
                    raise RuntimeError('injected restore failure')
                return original_insert(cur, table, row)

            monkeypatch.setattr(runtime.checkpoint, '_insert_row', fail_on_process_insert)
            with pytest.raises(RuntimeError, match='injected restore failure'):
                runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            restored = runtime.memory.get_object_by_name(pid, 'state')
            assert restored.payload == {'version': 2}
            assert runtime.store.get_object(temporary.oid) is not None
            assert finalizer_calls == []
            assert runtime.process.get(pid).status == ProcessStatus.RUNNABLE
            assert runtime.store.get_process_message(message.message_id).status == ProcessMessageStatus.UNREAD
            assert runtime.human.get(request_id).status == HumanRequestStatus.PENDING
        finally:
            runtime.close()

    @pytest.mark.parametrize(
        ('method_name', 'phase'),
        [
            ('_restore_images', 'image_reconciliation'),
            ('_restore_jit_sources', 'jit_source_reconciliation'),
            ('_prune_stale_ephemeral_jit_tools', 'jit_pruning'),
        ],
    )
    def test_restore_reports_post_commit_reconciliation_failure_without_claiming_rollback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        method_name: str,
        phase: str,
    ) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='post-commit restore failure')
            handle = runtime.memory.create_object(
                pid,
                ObjectType.SUMMARY,
                {'version': 1},
                ObjectMetadata(title='state'),
                immutable=False,
                name='post.commit.state',
            )
            checkpoint_id = runtime.checkpoint.create(pid, 'before post-commit failure', actor=pid)
            runtime.memory.update_object(pid, handle, ObjectPatch(payload={'version': 2}))

            def fail_reconciliation(*_args, **_kwargs):
                raise RuntimeError(f'injected {phase} failure')

            monkeypatch.setattr(runtime.checkpoint, method_name, fail_reconciliation)
            result = runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            assert runtime.memory.get_object_by_name(pid, 'post.commit.state').payload == {'version': 1}
            assert result['status'] == 'restored_with_warnings'
            assert result['main_state_committed'] is True
            assert [failure['phase'] for failure in result['post_commit_failures']] == [phase]
            failure_audits = [
                record
                for record in runtime.audit.trace()
                if record.action == 'checkpoint.restore.post_commit_failure'
            ]
            assert failure_audits[-1].decision['phase'] == phase
            assert failure_audits[-1].decision['main_state_committed'] is True
        finally:
            runtime.close()

    def test_restore_reports_release_finalizer_failure_after_main_state_commit(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='finalizer failure')
            checkpoint_id = runtime.checkpoint.create(pid, 'before temporary object', actor=pid)
            temporary = runtime.memory.create_object(
                pid,
                ObjectType.SUMMARY,
                {'temporary': True},
                ObjectMetadata(title='temporary'),
                immutable=False,
                name='post.commit.temporary',
            )

            def fail_finalizer(_obj, _actor, _reason):
                raise RuntimeError('injected release finalizer failure')

            runtime.memory.bind_object_release_finalizer(fail_finalizer)
            result = runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            assert runtime.store.get_object(temporary.oid) is None
            assert result['status'] == 'restored_with_warnings'
            assert result['main_state_committed'] is True
            assert result['post_commit_failures'][0]['phase'] == 'object_release_finalizers'
            assert any(
                record.action == 'checkpoint.restore.post_commit_failure'
                and record.decision['phase'] == 'object_release_finalizers'
                for record in runtime.audit.trace()
            )
        finally:
            runtime.close()

    @pytest.mark.parametrize(
        ('sink', 'phase'),
        [
            ('event', 'restore_event_emission'),
            ('audit', 'restore_audit_recording'),
        ],
    )
    def test_restore_reports_event_and_audit_failures_after_main_state_commit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sink: str,
        phase: str,
    ) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal=f'{sink} failure after restore')
            state = runtime.memory.create_object(
                pid,
                ObjectType.SUMMARY,
                {'version': 1},
                ObjectMetadata(title='state'),
                immutable=False,
                name=f'{sink}.failure.state',
            )
            checkpoint_id = runtime.checkpoint.create(pid, f'before {sink} failure', actor=pid)
            runtime.memory.update_object(pid, state, ObjectPatch(payload={'version': 2}))

            if sink == 'event':
                original_emit = runtime.events.emit

                def fail_restore_event(event_type, *args, **kwargs):
                    if event_type == EventType.ROLLBACK:
                        raise RuntimeError('injected restore event failure')
                    return original_emit(event_type, *args, **kwargs)

                monkeypatch.setattr(runtime.events, 'emit', fail_restore_event)
            else:
                original_record = runtime.audit.record

                def fail_restore_audit(*args, **kwargs):
                    if kwargs.get('action') == 'checkpoint.restore':
                        raise RuntimeError('injected restore audit failure')
                    return original_record(*args, **kwargs)

                monkeypatch.setattr(runtime.audit, 'record', fail_restore_audit)

            result = runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            assert runtime.memory.get_object_by_name(pid, f'{sink}.failure.state').payload == {'version': 1}
            assert result['status'] == 'restored_with_warnings'
            assert result['main_state_committed'] is True
            assert phase in [failure['phase'] for failure in result['post_commit_failures']]
        finally:
            runtime.close()

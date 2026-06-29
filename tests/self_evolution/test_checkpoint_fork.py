from __future__ import annotations

import pytest

from agent_libos import AgentImage, Runtime
from agent_libos.models import CapabilityEffect, CapabilityRight, ObjectOwnerKind, ObjectRight, ObjectType, ProcessStatus, ResourceBudget
from agent_libos.models.exceptions import CapabilityDenied, ProcessWaitRequired, ResourceLimitExceeded


class TestCheckpointFork:

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

    def test_fork_from_checkpoint_remaps_object_task_result_owner_to_forked_process_result(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='fork object task result')
            result = runtime.memory.create_object(pid, ObjectType.SUMMARY, {'value': 7}, name='task-result')
            runtime.memory.transfer_owner(
                ObjectOwnerKind.PROCESS,
                pid,
                ObjectOwnerKind.OBJECT_TASK,
                'otask_original',
                [result.oid],
                actor='test',
                reason='simulate_object_task_result',
            )
            creator_handle = runtime.capability.handle_for_object(
                pid,
                result.oid,
                [ObjectRight.READ.value, ObjectRight.MATERIALIZE.value, ObjectRight.LINK.value],
                issued_by='object_task:otask_original',
            )
            runtime._add_handle_to_process_view(pid, creator_handle)
            checkpoint_id = runtime.checkpoint.create(pid, 'fork object task result', actor=pid)
            runtime.capability.grant(pid, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')

            forked = runtime.checkpoint.fork_from_checkpoint(pid, checkpoint_id)

            forked_obj = runtime.store.get_object(forked['object_map'][result.oid])
            assert forked_obj is not None
            assert forked_obj.owner_kind == ObjectOwnerKind.PROCESS_RESULT
            assert forked_obj.owner_id == forked['pid_map'][pid]
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
            runtime.capability.grant(parent, 'process:spawn', [CapabilityRight.WRITE], issued_by='test')
            child = runtime.spawn_child_process(parent, 'unfinished child')
            with pytest.raises(ProcessWaitRequired):
                runtime.process.wait(parent, child)
            assert runtime.process.get(parent).status == ProcessStatus.WAITING_EVENT
            checkpoint_id = runtime.checkpoint.create(parent, 'waiting fork point', actor=parent)
            runtime.capability.grant(parent, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            forked = runtime.checkpoint.fork_from_checkpoint(parent, checkpoint_id)
            fork_root = runtime.process.get(forked['fork_root_pid'])
            assert fork_root.status == ProcessStatus.RUNNABLE
            assert fork_root.status_message is None
        finally:
            runtime.close()

    def test_fork_from_checkpoint_rolls_back_rows_and_payloads_when_insert_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='fork rollback')
            original = runtime.memory.create_object(pid, ObjectType.SUMMARY, {'value': 7}, name='state')
            checkpoint_id = runtime.checkpoint.create(pid, 'fork rollback point', actor=pid)
            runtime.capability.grant(pid, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            before_pids = {process.pid for process in runtime.process.list()}
            original_insert = runtime.checkpoint._insert_row

            def fail_on_process_insert(cur, table, row):
                if table == 'processes':
                    raise RuntimeError('injected fork failure')
                return original_insert(cur, table, row)

            monkeypatch.setattr(runtime.checkpoint, '_insert_row', fail_on_process_insert)
            with pytest.raises(RuntimeError, match='injected fork failure'):
                runtime.checkpoint.fork_from_checkpoint(pid, checkpoint_id)

            assert {process.pid for process in runtime.process.list()} == before_pids
            assert runtime.memory.get_object(pid, original).payload == {'value': 7}
        finally:
            runtime.close()

    def test_fork_from_checkpoint_does_not_replace_current_image_without_image_write(self) -> None:
        runtime = Runtime.open('local')
        image_id = 'checkpoint-fork-image:v0'
        try:
            runtime.register_image(
                AgentImage(image_id=image_id, name='checkpoint-fork-image', system_prompt='snapshot prompt'),
                actor='test',
            )
            source = runtime.process.spawn(image=image_id, goal='checkpoint image source')
            checkpoint_id = runtime.checkpoint.create(source, 'image fork point', actor=source)
            actor = runtime.process.spawn(image='base-agent:v0', goal='checkpoint executor')
            runtime.capability.grant(actor, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            runtime.register_image(
                AgentImage(image_id=image_id, name='checkpoint-fork-image', system_prompt='current prompt'),
                actor='test',
                replace=True,
            )

            forked = runtime.checkpoint.fork_from_checkpoint(actor, checkpoint_id)

            assert runtime.process.get(forked['fork_root_pid']).image_id == image_id
            assert not runtime.capability.check(actor, runtime.image_registry.resource_for(image_id), CapabilityRight.WRITE)
            assert runtime.get_image(image_id).system_prompt == 'current prompt'
            stored = runtime.store.get_image(image_id)
            assert stored is not None
            assert stored[0].system_prompt == 'current prompt'
        finally:
            runtime.close()

    def test_fork_from_checkpoint_requires_image_write_to_restore_missing_image(self) -> None:
        runtime = Runtime.open('local')
        image_id = 'checkpoint-fork-missing-image:v0'
        try:
            runtime.register_image(
                AgentImage(image_id=image_id, name='checkpoint-fork-missing-image', system_prompt='snapshot prompt'),
                actor='test',
            )
            source = runtime.process.spawn(image=image_id, goal='checkpoint missing image source')
            checkpoint_id = runtime.checkpoint.create(source, 'missing image fork point', actor=source)
            actor = runtime.process.spawn(image='base-agent:v0', goal='checkpoint executor')
            runtime.capability.grant(actor, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            runtime.images.pop(image_id)
            runtime.store.delete_image(image_id)
            before_pids = {process.pid for process in runtime.process.list()}

            with pytest.raises(CapabilityDenied, match=f'image:{image_id}'):
                runtime.checkpoint.fork_from_checkpoint(actor, checkpoint_id)

            assert {process.pid for process in runtime.process.list()} == before_pids
            runtime.image_registry.grant_register(actor, image_id, issued_by='test')
            forked = runtime.checkpoint.fork_from_checkpoint(actor, checkpoint_id)

            assert runtime.process.get(forked['fork_root_pid']).image_id == image_id
            assert runtime.get_image(image_id).system_prompt == 'snapshot prompt'
            stored = runtime.store.get_image(image_id)
            assert stored is not None
            assert stored[0].system_prompt == 'snapshot prompt'
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

    def test_checkpoint_fork_child_root_attaches_to_requested_parent(self) -> None:
        runtime = Runtime.open('local')
        try:
            source_parent = runtime.process.spawn(image='base-agent:v0', goal='source parent')
            runtime.capability.grant(source_parent, 'process:spawn', [CapabilityRight.WRITE], issued_by='test')
            source_child = runtime.spawn_child_process(source_parent, 'source child')
            target_parent = runtime.process.spawn(image='base-agent:v0', goal='target parent')
            checkpoint_id = runtime.checkpoint.create(source_child, 'child root fork', actor=source_child)
            runtime.capability.grant(source_child, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            runtime.capability.grant(source_child, runtime.checkpoint.process_resource(target_parent), [CapabilityRight.ADMIN], issued_by='test')

            forked = runtime.checkpoint.fork_from_checkpoint(source_child, checkpoint_id, parent_pid=target_parent)

            assert runtime.process.get(forked['fork_root_pid']).parent_pid == target_parent
        finally:
            runtime.close()

    def test_checkpoint_fork_parent_child_budget_exhaustion_rolls_back(self) -> None:
        runtime = Runtime.open('local')
        try:
            owner = runtime.process.spawn(image='base-agent:v0', goal='owner')
            exhausted_parent = runtime.process.spawn(
                image='base-agent:v0',
                goal='exhausted parent',
                resource_budget=ResourceBudget(max_child_processes=0),
            )
            checkpoint_id = runtime.checkpoint.create(owner, 'budgeted fork', actor=owner)
            runtime.capability.grant(owner, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            runtime.capability.grant(owner, runtime.checkpoint.process_resource(exhausted_parent), [CapabilityRight.ADMIN], issued_by='test')
            before_pids = {process.pid for process in runtime.process.list()}

            with pytest.raises(ResourceLimitExceeded):
                runtime.checkpoint.fork_from_checkpoint(owner, checkpoint_id, parent_pid=exhausted_parent)

            assert {process.pid for process in runtime.process.list()} == before_pids
            assert runtime.process.get(exhausted_parent).resource_usage.child_processes == 0
        finally:
            runtime.close()

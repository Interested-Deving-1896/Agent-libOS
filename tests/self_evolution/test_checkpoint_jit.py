from __future__ import annotations

import threading

import pytest

from agent_libos import Runtime
from agent_libos.models import CapabilityRight, ObjectOwnerKind, ObjectType, ToolCandidateStatus
from agent_libos.models.exceptions import NotFound


class TestCheckpointJit:

    def test_checkpoint_fork_does_not_publish_process_before_jit_assets_are_ready(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='toolmaker-agent:v0', goal='atomic jit fork')
            source = 'export function run(args, libos) { return { ok: true }; }'
            candidate_id = runtime.tools.propose(
                pid,
                {
                    'name': 'atomic_echo',
                    'description': 'Atomic fork tool.',
                    'input_schema': {'type': 'object'},
                    'output_schema': {'type': 'object'},
                },
                source_code=source,
            )
            candidate = runtime.store.get_tool_candidate(candidate_id)
            candidate.status = ToolCandidateStatus.VALIDATED
            candidate.validation = {'ok': True, 'language': 'typescript'}
            runtime.store.update_tool_candidate(candidate)
            runtime.tools.register(pid, candidate_id)
            checkpoint_id = runtime.checkpoint.create(pid, 'atomic jit fork', actor=pid)
            runtime.capability.grant(
                pid,
                f'checkpoint:{checkpoint_id}',
                [CapabilityRight.EXECUTE],
                issued_by='test',
            )
            before_pids = {process.pid for process in runtime.process.list()}
            restore_entered = threading.Event()
            allow_restore = threading.Event()
            original_restore = runtime.checkpoint._restore_jit_sources
            outcome: dict[str, object] = {}

            def blocked_restore(snapshot):
                restore_entered.set()
                assert allow_restore.wait(timeout=2.0)
                return original_restore(snapshot)

            def fork() -> None:
                try:
                    outcome['result'] = runtime.checkpoint.fork_from_checkpoint(pid, checkpoint_id)
                except BaseException as exc:  # pragma: no cover - asserted below
                    outcome['error'] = exc

            monkeypatch.setattr(runtime.checkpoint, '_restore_jit_sources', blocked_restore)
            worker = threading.Thread(target=fork)
            worker.start()
            assert restore_entered.wait(timeout=2.0)
            try:
                assert {process.pid for process in runtime.process.list()} == before_pids
            finally:
                allow_restore.set()
                worker.join(timeout=2.0)

            assert not worker.is_alive()
            assert 'error' not in outcome
            result = outcome['result']
            assert isinstance(result, dict)
            assert runtime.store.get_process(result['fork_root_pid']) is not None
        finally:
            runtime.close()

    def test_checkpoint_fork_clones_registered_jit_tool_and_candidate_identity(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='toolmaker-agent:v0', goal='jit checkpoint isolation')
            source = 'export function run(args, libos) { return { value: args.value }; }'
            candidate_id = runtime.tools.propose(
                pid,
                {
                    'name': 'isolated_echo',
                    'description': 'Echo a value.',
                    'input_schema': {'type': 'object', 'properties': {'value': {'type': 'string'}}},
                    'output_schema': {'type': 'object'},
                },
                source_code=source,
            )
            candidate = runtime.store.get_tool_candidate(candidate_id)
            candidate.status = ToolCandidateStatus.VALIDATED
            candidate.validation = {'ok': True, 'language': 'typescript'}
            runtime.store.update_tool_candidate(candidate)
            source_handle = runtime.tools.register(pid, candidate_id)
            process = runtime.store.get_process(pid)
            process.loaded_skills['skill:test-jit'] = {
                'tool_ids': {},
                'jit_tool_ids': {'isolated_echo': source_handle.tool_id},
            }
            runtime.store.update_process(process)
            checkpoint_id = runtime.checkpoint.create(pid, 'jit registered', actor=pid)
            runtime.capability.grant(
                pid,
                f'checkpoint:{checkpoint_id}',
                [CapabilityRight.EXECUTE],
                issued_by='test',
            )

            forked = runtime.checkpoint.fork_from_checkpoint(pid, checkpoint_id)
            fork_pid = forked['fork_root_pid']
            fork_handle = runtime.tools.resolve('isolated_echo', pid=fork_pid)

            assert fork_handle.tool_id != source_handle.tool_id
            assert runtime.tools._jit_sources[fork_handle.tool_id] == source
            fork_process = runtime.store.get_process(fork_pid)
            assert (
                fork_process.loaded_skills['skill:test-jit']['jit_tool_ids']['isolated_echo']
                == fork_handle.tool_id
            )
            fork_candidates = runtime.store.select_table_rows(
                'tool_candidates',
                'pid = ?',
                (fork_pid,),
            )
            assert len(fork_candidates) == 1
            assert fork_candidates[0]['candidate_id'] != candidate_id
            assert fork_candidates[0]['registered_tool_id'] == fork_handle.tool_id
            candidate_objects = [
                obj
                for obj in runtime.store.list_objects_owned_by(ObjectOwnerKind.PROCESS, fork_pid)
                if obj.type == ObjectType.TOOL_CANDIDATE
            ]
            assert len(candidate_objects) == 1
            assert candidate_objects[0].payload['candidate_id'] == fork_candidates[0]['candidate_id']

            unloaded = runtime.skills.unload_skill(
                fork_pid,
                'skill:test-jit',
                require_capability=False,
            )
            assert unloaded['removed_tools'] == ['isolated_echo']
            with pytest.raises(NotFound):
                runtime.tools.resolve('isolated_echo', pid=fork_pid)
            assert runtime.tools.resolve('isolated_echo', pid=pid).tool_id == source_handle.tool_id
            assert runtime.tools._jit_sources[source_handle.tool_id] == source
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
            with pytest.raises(NotFound):
                runtime.tools.resolve('echo_value')
            other = runtime.process.spawn(image='base-agent:v0', goal='cannot import restored JIT')
            with pytest.raises(NotFound):
                runtime.tools.configure_process_tools(other, ['echo_value'], assigned_by='test')
            runtime.tools._jit_sources.pop(handle.tool_id)
            runtime.tools._handles.pop(handle.tool_id)
            runtime.capability.grant(pid, f'checkpoint:{checkpoint_id}', [CapabilityRight.EXECUTE], issued_by='test')
            forked = runtime.checkpoint.fork_from_checkpoint(pid, checkpoint_id)
            fork_handle = runtime.tools.resolve('echo_value', pid=forked['fork_root_pid'])
            assert fork_handle.tool_id != handle.tool_id
            assert runtime.tools._jit_sources[fork_handle.tool_id] == source
        finally:
            runtime.close()

    def test_checkpoint_restore_prunes_post_checkpoint_ephemeral_jit_tool(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='toolmaker-agent:v0', goal='jit checkpoint prune')
            checkpoint_id = runtime.checkpoint.create(pid, 'before jit registration', actor=pid)
            source = 'export function run(args, libos) { return { value: args.value }; }'
            candidate_id = runtime.tools.propose(
                pid,
                {
                    'name': 'late_echo_value',
                    'description': 'Echo a value.',
                    'input_schema': {'type': 'object', 'properties': {'value': {'type': 'string'}}},
                    'output_schema': {'type': 'object'},
                },
                source_code=source,
            )
            candidate = runtime.store.get_tool_candidate(candidate_id)
            candidate.status = ToolCandidateStatus.VALIDATED
            candidate.validation = {'ok': True, 'language': 'typescript'}
            runtime.store.update_tool_candidate(candidate)
            handle = runtime.tools.register(pid, candidate_id)

            runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)

            assert handle.tool_id not in runtime.tools._jit_sources
            assert handle.tool_id not in runtime.tools._handles
            assert handle.tool_id not in {row['tool_id'] for row in runtime.store.list_tools()}
            with pytest.raises(NotFound):
                runtime.tools.resolve('late_echo_value', pid=pid)
        finally:
            runtime.close()

from __future__ import annotations

import pytest

from agent_libos import Runtime
from agent_libos.models import CapabilityRight, ToolCandidateStatus
from agent_libos.models.exceptions import NotFound


class TestCheckpointJit:

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
            assert runtime.tools._jit_sources[handle.tool_id] == source
            assert runtime.tools.resolve('echo_value', pid=forked['fork_root_pid']).tool_id == handle.tool_id
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

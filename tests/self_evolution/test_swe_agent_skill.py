from __future__ import annotations
import pytest
from pathlib import Path
from agent_libos import Runtime
from agent_libos.models import CapabilityRight
from agent_libos.tools.sandbox import DenoTypescriptSandbox

class TestSWEAgentSkill:

    @pytest.mark.real_deno
    def test_swe_agent_skill_registers_and_loads_without_granting_resource_authority(self) -> None:
        runtime = Runtime.open('local')
        try:
            registered = runtime.register_skill_from_path(Path('skills/swe-agent'), actor='cli', source_type='workspace')
            pid = runtime.process.spawn(image='base-agent:v0', goal='fix issue like SWE-Agent')
            runtime.capability.grant(pid, 'skill:swe-agent', [CapabilityRight.EXECUTE], issued_by='test')
            loaded = runtime.skills.activate_skill(pid, 'swe-agent', actor=pid)
            process = runtime.process.get(pid)
            assert registered['skill_id'] == 'swe-agent'
            for name in ['swe_view', 'swe_grep', 'swe_edit', 'swe_run', 'swe_submit']:
                assert name in loaded['jit_tool_ids']
                assert name in process.tool_table
            assert 'run_shell_command' in process.tool_table
            assert not runtime.capability.check(pid, 'filesystem:workspace:*', CapabilityRight.READ)
            assert not runtime.capability.check(pid, 'filesystem:workspace:*', CapabilityRight.WRITE)
            assert not runtime.capability.check(pid, 'shell:*', CapabilityRight.EXECUTE)
        finally:
            runtime.close()

    @pytest.mark.real_deno
    def test_swe_edit_refuses_truncated_source_before_write(self) -> None:
        source = Path('skills/swe-agent/scripts/swe_edit.ts').read_text(encoding='utf-8')
        sandbox = DenoTypescriptSandbox(deno_executable='deno')

        validation = sandbox.run_tests(
            source,
            [
                {
                    'args': {'path': 'large.txt', 'old_text': 'needle', 'new_text': 'replacement'},
                    'syscalls': [
                        {
                            'name': 'filesystem.read_text',
                            'args': {'path': 'large.txt', 'max_bytes': 1048576},
                            'result': {
                                'path': 'large.txt',
                                'content': 'needle and a partial file',
                                'truncated': True,
                            },
                        }
                    ],
                }
            ],
        )

        assert not validation.ok
        assert any('truncated' in error.lower() for error in validation.errors)

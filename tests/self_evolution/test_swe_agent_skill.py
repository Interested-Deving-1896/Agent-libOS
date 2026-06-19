from __future__ import annotations
import pytest
from pathlib import Path
from typing import Any
from agent_libos import Runtime
from agent_libos.models import CapabilityRight, ValidationResult
from agent_libos.substrate import SubprocessLimits
from agent_libos.tools.sandbox import DenoTypescriptSandbox, SandboxBackend, SyscallHandler

class TestSWEAgentSkill:

    def test_swe_agent_skill_registers_and_loads_without_granting_resource_authority(self) -> None:
        runtime = Runtime.open('local')
        runtime.tools.sandbox = StaticOnlyTypescriptSandbox()
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

class StaticOnlyTypescriptSandbox(SandboxBackend):
    language = 'typescript'

    def __init__(self) -> None:
        self.checker = DenoTypescriptSandbox(deno_executable='deno')

    def static_check(self, source_code: str) -> ValidationResult:
        return self.checker.static_check(source_code)

    async def arun_source(self, source_code: str, args: dict[str, Any], *, pid: str | None=None, syscall_handler: SyscallHandler | None=None, timeout: float | None=None) -> Any:
        return {'ok': True}

    def run_tests(
        self,
        source_code: str,
        tests: list[dict[str, Any]],
        timeout: float | None = None,
        *,
        limits: SubprocessLimits | None = None,
        return_metrics: bool = False,
    ) -> ValidationResult:
        validation = self.static_check(source_code)
        if not validation.ok:
            return validation
        metadata = {}
        if return_metrics:
            metadata['metrics'] = {
                'wall_seconds': 0.0,
                'cpu_seconds': 0.0,
                'peak_memory_bytes': 0,
                'killed': False,
                'limit_kind': None,
            }
        return ValidationResult(ok=True, logs='static-only TypeScript validation', metadata=metadata)

    def metadata_for_source(self, source_code: str) -> dict[str, Any]:
        return {'language': 'typescript', 'imports': [], 'validation': 'static-only'}

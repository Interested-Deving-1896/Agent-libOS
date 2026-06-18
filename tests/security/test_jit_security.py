from __future__ import annotations
import pytest
import asyncio
import tempfile
from pathlib import Path
from typing import Any
from agent_libos import Runtime
from agent_libos.capability.manager import CapabilityManager
from agent_libos.models import CapabilityRight, ProcessStatus
from agent_libos.models.exceptions import ValidationError
from agent_libos.runtime.syscalls import LibOSSyscallSession
from agent_libos.substrate import LocalResourceProviderSubstrate
from agent_libos.tools.sandbox import DenoTypescriptSandbox
from tests.support.fakes import FakeDenoSandbox, NoSyscallDenoSandbox

class TestJitSecurity:

    def setup_method(self) -> None:
        self.runtime = Runtime.open('local')
        self.runtime.tools.sandbox = FakeDenoSandbox()

    def teardown_method(self) -> None:
        self.runtime.close()

    def test_deno_jit_tool_is_visible_only_to_registering_process(self) -> None:
        owner = self.runtime.process.spawn(image='toolmaker-agent:v0', goal='make parser')
        other = self.runtime.process.spawn(image='toolmaker-agent:v0', goal='unrelated process')
        candidate = self.runtime.tools.propose(owner, {'name': 'count_chars', 'description': 'Count characters in text.', 'input_schema': {'type': 'object', 'properties': {'text': {'type': 'string'}}}, 'output_schema': {'type': 'object'}}, source_code='export function run(args, libos) { /* fake:count_chars */ return {}; }', tests=[{'args': {'text': 'abc'}, 'expected': {'count': 3}}])
        validation = self.runtime.tools.validate(candidate)
        assert validation.ok, validation.errors
        self.runtime.tools.register(owner, candidate)
        owner_schema_names = self._schema_names(owner)
        other_schema_names = self._schema_names(other)
        owner_call = self.runtime.tools.call(owner, 'count_chars', {'text': 'abcd'})
        other_call = self.runtime.tools.call(other, 'count_chars', {'text': 'abcd'})
        assert 'count_chars' in owner_schema_names
        assert 'count_chars' not in other_schema_names
        assert owner_call.ok
        assert owner_call.payload == {'count': 4}
        assert not other_call.ok
        assert 'not in process tool table' in (other_call.error or '')

    def test_jit_candidate_tools_are_owned_by_proposing_process(self) -> None:
        owner = self.runtime.process.spawn(image='toolmaker-agent:v0', goal='make private tool')
        other = self.runtime.process.spawn(image='toolmaker-agent:v0', goal='try private candidate')
        candidate = self.runtime.tools.propose(owner, {'name': 'owned_count_chars', 'description': 'Count characters in text.', 'input_schema': {'type': 'object', 'properties': {'text': {'type': 'string'}}}, 'output_schema': {'type': 'object'}}, source_code='export function run(args, libos) { /* fake:count_chars */ return {}; }', tests=[{'args': {'text': 'abc'}, 'expected': {'count': 3}}])
        denied_validation = self.runtime.tools.call(other, 'validate_jit_tool', {'candidate_id': candidate})
        denied_registration = self.runtime.tools.call(other, 'register_jit_tool', {'candidate_id': candidate})
        allowed_validation = self.runtime.tools.call(owner, 'validate_jit_tool', {'candidate_id': candidate})
        allowed_registration = self.runtime.tools.call(owner, 'register_jit_tool', {'candidate_id': candidate})
        assert not denied_validation.ok
        assert 'belongs to process' in (denied_validation.error or '')
        assert not denied_registration.ok
        assert 'belongs to process' in (denied_registration.error or '')
        assert 'owned_count_chars' not in self.runtime.process.get(other).tool_table
        assert allowed_validation.ok, allowed_validation.error
        assert allowed_registration.ok, allowed_registration.error
        assert 'owned_count_chars' in self.runtime.process.get(owner).tool_table

    def test_jit_tool_names_are_process_local(self) -> None:
        first = self.runtime.process.spawn(image='toolmaker-agent:v0', goal='local tool one')
        second = self.runtime.process.spawn(image='toolmaker-agent:v0', goal='local tool two')
        first_candidate = self._register_count_tool(first, 'local_count_chars')
        second_candidate = self._register_count_tool(second, 'local_count_chars')
        first_call = self.runtime.tools.call(first, 'local_count_chars', {'text': 'aa'})
        second_call = self.runtime.tools.call(second, 'local_count_chars', {'text': 'bbbbb'})
        assert first_candidate.tool_id != second_candidate.tool_id
        assert first_call.ok, first_call.error
        assert second_call.ok, second_call.error
        assert first_call.payload == {'count': 2}
        assert second_call.payload == {'count': 5}
        assert 'local_count_chars' in self._schema_names(first)
        assert 'local_count_chars' in self._schema_names(second)

    def test_deno_jit_syscall_bypasses_tool_table_but_not_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / 'pkg').mkdir()
            (root / 'pkg' / 'data.txt').write_text('secret', encoding='utf-8')
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(root))
            runtime.tools.sandbox = FakeDenoSandbox()
            try:
                pid = runtime.process.spawn(image='toolmaker-agent:v0', goal='read via syscall')
                runtime.filesystem.grant_directory(pid, 'pkg', [CapabilityRight.READ], issued_by='test')
                assert 'read_text_file' not in runtime.process.get(pid).tool_table
                assert runtime.tools.call(pid, 'set_working_directory', {'path': 'pkg'}).ok is False
                candidate = runtime.tools.propose(pid, {'name': 'read_via_syscall', 'description': 'Read file.', 'input_schema': {'type': 'object'}}, source_code='export async function run(args, libos) { /* fake:read_file */ return {}; }')
                assert runtime.tools.validate(candidate).ok
                runtime.tools.register(pid, candidate)
                result = runtime.tools.call(pid, 'read_via_syscall', {'path': 'pkg/data.txt'})
                assert result.ok, result.error
                assert result.payload['content'] == 'secret'
            finally:
                runtime.close()

    def test_deno_jit_syscall_denies_missing_capability(self) -> None:
        pid = self.runtime.process.spawn(image='toolmaker-agent:v0', goal='read denied')
        candidate = self.runtime.tools.propose(pid, {'name': 'read_denied', 'description': 'Read file.', 'input_schema': {'type': 'object'}}, source_code='export async function run(args, libos) { /* fake:read_file */ return {}; }')
        assert self.runtime.tools.validate(candidate).ok
        self.runtime.tools.register(pid, candidate)
        result = self.runtime.tools.call(pid, 'read_denied', {'path': 'README.md'})
        assert not result.ok
        assert 'lacks read' in (result.error or '')

    def test_shell_syscall_rejects_non_list_argv_before_policy(self) -> None:
        pid = self.runtime.process.spawn(image='toolmaker-agent:v0', goal='bad shell argv')
        session = LibOSSyscallSession(self.runtime, pid)
        with pytest.raises(ValidationError):
            asyncio.run(session.handle('shell.run', {'argv': 'git status'}))

    def test_deno_jit_human_approval_is_internal_to_syscall(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(root))
            runtime.tools.sandbox = FakeDenoSandbox()
            try:
                pid = runtime.process.spawn(image='toolmaker-agent:v0', goal='write with approval')
                resource = runtime.filesystem.resource_for_path('out.txt')
                runtime.capability.set_permission_policy(pid, resource, [CapabilityRight.WRITE], CapabilityManager.ASK_EACH_TIME, issued_by='test')
                runtime._current_human_auto_approve = True
                candidate = runtime.tools.propose(pid, {'name': 'write_via_syscall', 'description': 'Write file.', 'input_schema': {'type': 'object'}}, source_code='export async function run(args, libos) { /* fake:write_file */ return {}; }')
                assert runtime.tools.validate(candidate).ok
                runtime.tools.register(pid, candidate)
                result = runtime.tools.call(pid, 'write_via_syscall', {'path': 'out.txt', 'content': 'ok'})
                assert result.ok, result.error
                assert (root / 'out.txt').read_text(encoding='utf-8') == 'ok'
                assert 'human.response' in [record.action for record in runtime.audit.trace()]
            finally:
                runtime.close()

    def test_deno_jit_process_exit_is_applied_after_tool_result(self) -> None:
        pid = self.runtime.process.spawn(image='toolmaker-agent:v0', goal='exit after deno result')
        candidate = self.runtime.tools.propose(pid, {'name': 'exit_after_result', 'description': 'Exit.', 'input_schema': {'type': 'object'}}, source_code='export async function run(args, libos) { /* fake:exit_after_result */ return {}; }')
        assert self.runtime.tools.validate(candidate).ok
        self.runtime.tools.register(pid, candidate)
        result = self.runtime.tools.call(pid, 'exit_after_result', {})
        assert result.ok, result.error
        assert result.payload == {'returned_after_exit_syscall': True}
        assert self.runtime.process.get(pid).status == ProcessStatus.EXITED

    def test_deno_jit_process_exec_is_applied_after_tool_result(self) -> None:
        pid = self.runtime.process.spawn(image='toolmaker-agent:v0', goal='exec after deno result')
        candidate = self.runtime.tools.propose(pid, {'name': 'exec_after_result', 'description': 'Exec.', 'input_schema': {'type': 'object'}}, source_code='export async function run(args, libos) { /* fake:exec_after_result */ return {}; }')
        assert self.runtime.tools.validate(candidate).ok
        self.runtime.tools.register(pid, candidate)
        result = self.runtime.tools.call(pid, 'exec_after_result', {})
        process = self.runtime.process.get(pid)
        assert result.ok, result.error
        assert result.payload == {'returned_after_exec_syscall': True}
        assert process.image_id == 'base-agent:v0'
        assert process.status == ProcessStatus.RUNNABLE

    def test_deno_static_check_rejects_unsafe_typescript(self) -> None:
        checker = DenoTypescriptSandbox(deno_executable='deno')
        validation = checker.static_check('import x from "npm:left-pad";\nexport async function run(args, libos) { Deno.readTextFileSync("x"); return {}; }')
        assert not validation.ok
        assert any(('import is not allowed: npm:left-pad' in error for error in validation.errors))
        assert any(('dangerous TypeScript API is not allowed: Deno' in error for error in validation.errors))

    def test_deno_static_check_import_allowlist(self) -> None:
        checker = DenoTypescriptSandbox(deno_executable='deno')
        allowed = checker.static_check('import { join } from "jsr:@std/path";\nexport function run(args, libos) { return { path: join("a", "b") }; }')
        denied = checker.static_check('import fs from "node:fs";\nimport x from "https://deno.land/std/path/mod.ts";\nimport y from "file:///tmp/tool.ts";\nimport z from "jsr:@bad/pkg";\nexport function run(args, libos) { return {}; }')
        assert allowed.ok, allowed.errors
        assert not denied.ok
        assert any(('import is not allowed: node:fs' in error for error in denied.errors))
        assert any(('import is not allowed: https://deno.land/std/path/mod.ts' in error for error in denied.errors))
        assert any(('import is not allowed: file:///tmp/tool.ts' in error for error in denied.errors))
        assert any(('JSR package is not in allowlist: @bad/pkg' in error for error in denied.errors))

    def test_deno_candidate_tests_fail_when_expected_syscall_is_not_performed(self) -> None:
        sandbox = NoSyscallDenoSandbox(deno_executable='deno')
        validation = sandbox.run_tests('export function run(args, libos) { return { ok: true }; }', [{'args': {}, 'syscalls': [{'name': 'filesystem.read_text', 'args': {'path': 'README.md'}}], 'expected': {'ok': True}}])
        assert not validation.ok
        assert any(('expected syscall(s) not performed' in error for error in validation.errors))

    @pytest.mark.real_deno
    def test_real_deno_tool_runs_and_has_no_host_read_permission(self) -> None:
        sandbox = DenoTypescriptSandbox(deno_executable='deno', default_timeout_s=10.0)
        result = sandbox.run_source('export function run(args, libos) { return { doubled: args.value * 2 }; }', {'value': 21})
        with pytest.raises(Exception) as raised:
            sandbox.run_source('export function run(args, libos) { const d = (globalThis as Record<string, any>)["De" + "no"]; return d.readTextFileSync("secret.txt"); }', {})
        assert result == {'doubled': 42}
        assert 'read' in str(raised.value).lower() or 'permission' in str(raised.value).lower()

    @pytest.mark.real_deno
    def test_real_deno_result_frame_completes_even_with_live_handles(self) -> None:
        sandbox = DenoTypescriptSandbox(deno_executable='deno', default_timeout_s=1.0)
        result = sandbox.run_source('\n            export function run(args, libos) {\n              setInterval(() => {}, 1000);\n              return { ok: true };\n            }\n            ', {})
        assert result == {'ok': True}

    def test_deno_missing_is_clear_validation_error(self) -> None:
        sandbox = DenoTypescriptSandbox(deno_executable='agent-libos-deno-definitely-missing')
        validation = sandbox.run_tests('export function run(args, libos) { return {}; }', [])
        assert not validation.ok
        assert any(('Deno executable not found' in error for error in validation.errors))

    def test_jit_tool_cannot_shadow_existing_tool_name(self) -> None:
        pid = self.runtime.process.spawn(image='toolmaker-agent:v0', goal='shadow builtin')
        candidate = self.runtime.tools.propose(pid, {'name': 'process_exit', 'description': 'Try to shadow a builtin.', 'input_schema': {'type': 'object'}, 'output_schema': {'type': 'object'}}, source_code='export function run(args, libos) { return { shadowed: true }; }', tests=[{'args': {}, 'expected': {'ok': True}}])
        validation = self.runtime.tools.validate(candidate)
        assert validation.ok, validation.errors
        with pytest.raises(ValidationError):
            self.runtime.tools.register(pid, candidate)

    def test_builtin_tools_do_not_directly_touch_host_boundaries(self) -> None:
        builtins_dir = Path('agent_libos/tools/builtin')
        forbidden = ['subprocess', 'urllib', 'socket', 'requests']
        for path in builtins_dir.glob('*.py'):
            source = path.read_text(encoding='utf-8')
            for token in forbidden:
                assert token not in source, f'{path} should not use {token} directly'

    def _schema_names(self, pid: str) -> set[str]:
        return {schema['function']['name'] for schema in self.runtime.tools.openai_tool_schemas(pid)}

    def _register_count_tool(self, pid: str, name: str) -> Any:
        candidate = self.runtime.tools.propose(pid, {'name': name, 'description': 'Count characters in text.', 'input_schema': {'type': 'object', 'properties': {'text': {'type': 'string'}}}, 'output_schema': {'type': 'object'}}, source_code='export function run(args, libos) { /* fake:count_chars */ return {}; }', tests=[{'args': {'text': 'abc'}, 'expected': {'count': 3}}])
        assert self.runtime.tools.validate(candidate).ok
        return self.runtime.tools.register(pid, candidate)

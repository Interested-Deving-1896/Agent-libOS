from __future__ import annotations
import pytest
import contextlib
import io
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any
from agent_libos import AgentImage, Runtime
from agent_libos.api.cli import main as cli_main
from agent_libos.config import AgentLibOSConfig, SkillDefaults
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import CapabilityRight, ValidationResult
from agent_libos.models.exceptions import CapabilityDenied, HumanApprovalRequired, NotFound, ValidationError
from agent_libos.runtime.syscalls import LibOSSyscallSession
from agent_libos.substrate import LocalResourceProviderSubstrate
from agent_libos.tools.sandbox import DenoTypescriptSandbox, SandboxBackend, SyscallHandler

class TestSkillDynamicLoading:

    def test_standard_package_validation_and_global_trust(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            global_dir = root / 'global-skills'
            skill_dir = _write_skill_package(global_dir, 'global-skill', allowed_tools=['echo'])
            config = AgentLibOSConfig(skills=replace(SkillDefaults(), global_dirs=(str(global_dir),)))
            runtime = Runtime.open('local', config=config)
            try:
                with pytest.raises(CapabilityDenied):
                    runtime.skills.register_global_skill_from_path(skill_dir, actor='cli', require_capability=False)
                trust = runtime.skills.global_package_info(skill_dir)
                runtime.skills.trust_skill_source(actor='cli', source_type='global', source=trust['source'], package_sha256=trust['package_sha256'], require_capability=False)
                registered = runtime.skills.register_global_skill_from_path(skill_dir, actor='cli', require_capability=False)
                assert registered['skill_id'] == 'global-skill'
                assert registered['source_type'] == 'global'
                assert 'package_sha256' in registered
                with pytest.raises(ValidationError):
                    runtime.skills.validate_package_path(_write_raw_skill(root, 'bad', 'name: bad\ndescription: Bad\nunknown: nope\n'))
                with pytest.raises(ValidationError):
                    runtime.skills.validate_package_path(_write_raw_skill(root, 'BadName', 'name: BadName\ndescription: Bad\n'))
                with pytest.raises(ValidationError):
                    runtime.skills.validate_package_path(_write_raw_skill(root, 'bad-metadata', 'name: bad-metadata\ndescription: Bad\nmetadata: {agent-libos.version: 1}\n'))
                old_yaml = root / 'legacy.yaml'
                old_yaml.write_text('schema_version: 1\nskill_id: legacy:v0\nname: Legacy\n', encoding='utf-8')
                with pytest.raises(ValidationError):
                    runtime.skills.validate_package_path(old_yaml)
                with pytest.raises(ValidationError):
                    runtime.skills.register_skill_package({'schema_version': 1, 'skill_id': 'legacy', 'name': 'legacy', 'description': 'Legacy shape.', 'tools': ['echo']}, actor='cli', require_capability=False)
                with pytest.raises(ValidationError):
                    _write_skill_package(root, 'bad-jit', jit_tools=[{'name': 'bad', 'description': 'bad', 'source_path': '../escaped.ts'}])
                with pytest.raises(ValidationError):
                    _write_skill_package(root, 'bad-right', required_capabilities=[{'resource': 'filesystem:workspace:*', 'rights': ['*']}])
            finally:
                runtime.close()

    def test_workspace_register_and_activate_reads_via_filesystem_and_uses_human_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_skill_package(Path(temp_dir), 'workspace-skill', allowed_tools=['echo'], extra_resources={'references/guide.md': 'Workspace resource guide.'})
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='load workspace skill')
                runtime.filesystem.grant_path(pid, 'workspace-skill/SKILL.md', [CapabilityRight.READ], issued_by='test')
                runtime.filesystem.grant_directory(pid, 'workspace-skill/references', [CapabilityRight.READ], issued_by='test')
                with pytest.raises(HumanApprovalRequired) as raised:
                    runtime.skills.activate_skill_from_workspace_path(pid, 'workspace-skill')
                runtime.human.approve(raised.value.request_id)
                loaded = runtime.skills.activate_skill_from_workspace_path(pid, 'workspace-skill')
                assert loaded['skill_id'] == 'workspace-skill'
                assert 'echo' in runtime.process.get(pid).tool_table
                assert not runtime.capability.check(pid, 'skill:workspace-skill', CapabilityRight.EXECUTE)
                resource = runtime.skills.read_skill_resource(pid, 'workspace-skill', 'references/guide.md')
                assert resource['content'] == 'Workspace resource guide.'
            finally:
                runtime.close()

    def test_skill_syscalls_use_primitive_capabilities_not_tool_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_skill_package(Path(temp_dir), 'syscall-skill', allowed_tools=['echo'])
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='syscall skill')
                process = runtime.process.get(pid)
                process.tool_table.pop('activate_skill', None)
                runtime.store.update_process(process)
                runtime.filesystem.grant_path(pid, 'syscall-skill/SKILL.md', [CapabilityRight.READ], issued_by='test')
                runtime.capability.grant(pid, 'skill:syscall-skill', [CapabilityRight.WRITE, CapabilityRight.EXECUTE], issued_by='test')
                registered = self._run(LibOSSyscallSession(runtime, pid).handle('skill.register_path', {'path': 'syscall-skill'}))
                loaded = self._run(LibOSSyscallSession(runtime, pid).handle('skill.activate', {'skill_id': 'syscall-skill'}))
                assert registered['skill_id'] == 'syscall-skill'
                assert loaded['skill_id'] == 'syscall-skill'
                assert 'echo' in runtime.process.get(pid).tool_table
                with pytest.raises(NotFound):
                    self._run(LibOSSyscallSession(runtime, pid).handle('skill.register', {'skill': {'schema_version': 1, 'skill_id': 'inline-skill', 'name': 'inline-skill', 'description': 'Inline package should not be syscall-visible.', 'instructions': 'inline'}}))
            finally:
                runtime.close()

    def test_loaded_existing_tool_visibility_does_not_grant_resource_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = _write_skill_package(Path(temp_dir), 'read-skill', allowed_tools=['read_text_file'])
            runtime = Runtime.open('local')
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='load read tool')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                runtime.capability.grant(pid, 'skill:read-skill', [CapabilityRight.EXECUTE], issued_by='test')
                runtime.skills.activate_skill(pid, 'read-skill', actor=pid)
                result = runtime.tools.call(pid, 'read_text_file', {'path': 'secret.txt'})
                assert 'read_text_file' in runtime.process.get(pid).tool_table
                assert not runtime.capability.check(pid, 'filesystem:workspace:secret.txt', CapabilityRight.READ)
                assert not result.ok
                assert 'lacks read' in (result.error or '')
            finally:
                runtime.close()

    def test_read_skill_resource_requires_loaded_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = _write_skill_package(Path(temp_dir), 'resource-skill', allowed_tools=['echo'], extra_resources={'references/guide.md': 'Remember resource-token.\n'})
            runtime = Runtime.open('local')
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='read resource')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                with pytest.raises(CapabilityDenied):
                    runtime.skills.read_skill_resource(pid, 'resource-skill', 'references/guide.md')
                runtime.capability.grant(pid, 'skill:resource-skill', [CapabilityRight.EXECUTE], issued_by='test')
                runtime.skills.activate_skill(pid, 'resource-skill', actor=pid)
                resource = runtime.skills.read_skill_resource(pid, 'resource-skill', 'references/guide.md')
                assert 'resource-token' in resource['content']
            finally:
                runtime.close()

    def test_cross_process_skill_activate_requires_target_process_admin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = _write_skill_package(Path(temp_dir), 'cross-load-skill', allowed_tools=['echo'])
            runtime = Runtime.open('local')
            try:
                actor = runtime.process.spawn(image='base-agent:v0', goal='actor')
                target = runtime.process.spawn(image='base-agent:v0', goal='target')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                runtime.capability.grant(actor, 'skill:cross-load-skill', [CapabilityRight.EXECUTE], issued_by='test')
                with pytest.raises(CapabilityDenied):
                    runtime.skills.activate_skill(target, 'cross-load-skill', actor=actor)
                runtime.capability.grant(actor, f'process:{target}', [CapabilityRight.ADMIN], issued_by='test')
                loaded = runtime.skills.activate_skill(target, 'cross-load-skill', actor=actor)
                assert loaded['pid'] == target
                assert 'echo' in runtime.process.get(target).tool_table
            finally:
                runtime.close()

    def test_unload_skill_consumes_one_time_execute_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = _write_skill_package(Path(temp_dir), 'unload-skill', allowed_tools=['echo'])
            runtime = Runtime.open('local')
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='unload skill')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                runtime.activate_skill(pid, 'unload-skill')
                runtime.capability.grant_once(pid, 'skill:unload-skill', [CapabilityRight.EXECUTE], issued_by='test')
                runtime.skills.unload_skill(pid, 'unload-skill', actor=pid)
                assert not runtime.capability.check(pid, 'skill:unload-skill', CapabilityRight.EXECUTE)
                assert 'echo' not in runtime.process.get(pid).tool_table
            finally:
                runtime.close()

    def test_jit_skill_tool_is_process_local_and_uses_deno_validation_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = _write_skill_package(Path(temp_dir), 'jit-skill', jit_tools=[{'name': 'skill_count', 'description': 'Count text characters.', 'source_path': 'scripts/skill_count.ts', 'input_schema': {'type': 'object'}, 'output_schema': {'type': 'object'}, 'tests': [{'args': {'text': 'abc'}, 'expected': {'count': 3}}]}], scripts={'scripts/skill_count.ts': 'export function run(args, libos) { /* fake:count_chars */ return {}; }\n'})
            runtime = Runtime.open('local')
            runtime.tools.sandbox = FakeSkillDenoSandbox()
            try:
                owner = runtime.process.spawn(image='base-agent:v0', goal='load jit skill')
                other = runtime.process.spawn(image='base-agent:v0', goal='other')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                runtime.capability.grant(owner, 'skill:jit-skill', [CapabilityRight.EXECUTE], issued_by='test')
                loaded = runtime.skills.activate_skill(owner, 'jit-skill', actor=owner)
                result = runtime.tools.call(owner, 'skill_count', {'text': 'hello'})
                assert 'skill_count' in loaded['jit_tool_ids']
                assert result.ok, result.error
                assert result.payload == {'count': 5}
                assert 'skill_count' in runtime.process.get(owner).tool_table
                assert 'skill_count' not in runtime.process.get(other).tool_table
            finally:
                runtime.close()

    def test_image_default_skills_spawn_fork_spawn_child_and_exec_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_skill = _write_skill_package(Path(temp_dir), 'image-skill', allowed_tools=['echo'])
            extra_skill = _write_skill_package(Path(temp_dir), 'parent-extra', allowed_tools=['read_text_file'])
            runtime = Runtime.open('local')
            try:
                runtime.skills.register_skill_from_path(image_skill, actor='cli', require_capability=False)
                runtime.skills.register_skill_from_path(extra_skill, actor='cli', require_capability=False)
                runtime.register_image(AgentImage(image_id='skill-image:v0', name='skill-image', default_tools=['human_output'], default_skills=['image-skill']), actor='cli')
                root = runtime.process.spawn(image='skill-image:v0', goal='root')
                runtime.capability.grant(root, 'skill:parent-extra', [CapabilityRight.EXECUTE], issued_by='test')
                runtime.skills.activate_skill(root, 'parent-extra', actor=root)
                forked = runtime.process.fork(root, 'forked')
                spawned = runtime.spawn_child_process(root, 'spawned', image='base-agent:v0')
                runtime.exec_process(spawned, 'skill-image:v0', goal='exec')
                assert 'echo' in runtime.process.get(root).tool_table
                assert 'read_text_file' in runtime.process.get(forked).tool_table
                assert 'read_text_file' not in runtime.process.get(spawned).tool_table
                assert 'echo' in runtime.process.get(spawned).tool_table
            finally:
                runtime.close()

    def test_checkpoint_restore_preserves_loaded_skill_records_and_tool_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = _write_skill_package(Path(temp_dir), 'checkpoint-skill', allowed_tools=['read_text_file'])
            runtime = Runtime.open('local')
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='checkpoint skill')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                runtime.capability.grant(pid, 'skill:checkpoint-skill', [CapabilityRight.EXECUTE], issued_by='test')
                runtime.skills.activate_skill(pid, 'checkpoint-skill', actor=pid)
                checkpoint_id = runtime.checkpoint.create(pid, 'skill loaded', actor=pid)
                runtime.skills.unload_skill(pid, 'checkpoint-skill', actor=pid)
                runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)
                assert 'checkpoint-skill' in runtime.process.get(pid).loaded_skills
                assert 'read_text_file' in runtime.process.get(pid).tool_table
            finally:
                runtime.close()

    def test_skill_cli_outputs_stable_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = str(root / 'runtime.sqlite')
            skill_dir = _write_skill_package(root, 'cli-skill', allowed_tools=['echo'])
            validated = self._cli_json(['--db', db_path, 'skills', 'validate', str(skill_dir)])
            registered = self._cli_json(['--db', db_path, 'skills', 'register', str(skill_dir)])
            discovered = self._cli_json(['--db', db_path, 'skills', 'discover', '--text', 'cli'])
            spawned = self._cli_json(['--db', db_path, 'spawn', '--goal', 'cli skill'])
            loaded = self._cli_json(['--db', db_path, 'skills', 'activate', spawned['pid'], 'cli-skill'])
            assert validated['skill_id'] == 'cli-skill'
            assert registered['skill_id'] == 'cli-skill'
            assert discovered[0]['skill_id'] == 'cli-skill'
            assert loaded['skill_id'] == 'cli-skill'
            assert 'echo' in loaded['tool_names']

    def test_skill_cli_actor_pid_register_reads_workspace_package_through_primitive(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir).resolve()
            db_path = str(root / 'runtime.sqlite')
            skill_dir = _write_skill_package(root, 'cli-actor-skill', allowed_tools=['echo'])
            relative_skill = skill_dir.relative_to(Path.cwd().resolve()).as_posix()
            skill_md = f'{relative_skill}/SKILL.md'
            runtime = Runtime.open(db_path)
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='actor cli skill')
                runtime.capability.grant(pid, 'skill:cli-actor-skill', [CapabilityRight.WRITE], issued_by='test')
            finally:
                runtime.close()
            with pytest.raises(CapabilityDenied):
                self._cli_json(['--db', db_path, 'skills', '--actor-pid', pid, 'register', relative_skill])
            runtime = Runtime.open(db_path)
            try:
                runtime.filesystem.grant_path(pid, skill_md, [CapabilityRight.READ], issued_by='test')
            finally:
                runtime.close()
            registered = self._cli_json(['--db', db_path, 'skills', '--actor-pid', pid, 'register', relative_skill])
            assert registered['skill_id'] == 'cli-actor-skill'

    def test_loaded_skill_instructions_are_materialized_into_llm_prompt_and_persisted_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = _write_skill_package(Path(temp_dir), 'prompt-skill', allowed_tools=['echo'], body='Always preserve the phrase skill-instruction-token in planning context.\n', actions=[{'name': 'prompt_action', 'use_cases': ['prompt testing']}])
            runtime = Runtime.open('local')
            try:
                runtime.llm.client = RecordingActionClient([{'action': 'process_exit', 'payload': {'done': True}}])
                pid = runtime.process.spawn(image='base-agent:v0', goal='use skill prompt')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                runtime.capability.grant(pid, 'skill:prompt-skill', [CapabilityRight.EXECUTE], issued_by='test')
                runtime.skills.activate_skill(pid, 'prompt-skill', actor=pid)
                runtime.run_next_process_once()
                assert 'skill-instruction-token' in runtime.llm.client.user_prompts[0]
                persisted = runtime.store.list_llm_calls(pid)
                assert len(persisted) == 1
                assert 'skill-instruction-token' in persisted[0].messages[1]['content']
            finally:
                runtime.close()

    def _cli_json(self, argv: list[str]) -> Any:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            cli_main(argv)
        return json.loads(stdout.getvalue())

    def _run(self, awaitable: Any) -> Any:
        import asyncio
        return asyncio.run(awaitable)

class FakeSkillDenoSandbox(SandboxBackend):
    language = 'typescript'

    def __init__(self) -> None:
        self.checker = DenoTypescriptSandbox(deno_executable='deno')

    def static_check(self, source_code: str) -> ValidationResult:
        return self.checker.static_check(source_code)

    async def arun_source(self, source_code: str, args: dict[str, Any], *, pid: str | None=None, syscall_handler: SyscallHandler | None=None, timeout: float | None=None) -> Any:
        if 'fake:count_chars' in source_code:
            return {'count': len(str(args.get('text', '')))}
        return {'ok': True}

    def run_tests(self, source_code: str, tests: list[dict[str, Any]], timeout: float | None=None) -> ValidationResult:
        validation = self.static_check(source_code)
        if not validation.ok:
            return validation
        errors: list[str] = []
        for index, test in enumerate(tests, start=1):
            result = self.run_source(source_code, test.get('args', {}))
            if 'expected' in test and result != test['expected']:
                errors.append(f"test {index} expected {test['expected']!r}, got {result!r}")
        return ValidationResult(ok=not errors, errors=errors, logs='fake skill deno tests')

    def metadata_for_source(self, source_code: str) -> dict[str, Any]:
        return {'language': 'typescript', 'deno_version': 'fake-deno', 'imports': []}

class RecordingActionClient:

    def __init__(self, actions: list[dict[str, Any]]):
        self.actions = list(actions)
        self.user_prompts: list[str] = []

    def complete_action(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMCompletion:
        self.user_prompts.append(str(messages[-1]['content']))
        action = self.actions.pop(0)
        name = str(action['action'])
        args = {key: value for key, value in action.items() if key != 'action'}
        return LLMCompletion(content='', tool_calls=[{'id': 'skill_prompt', 'name': name, 'arguments': json.dumps(args)}])

def _write_raw_skill(root: Path, name: str, frontmatter: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / 'SKILL.md').write_text(f'---\n{frontmatter}---\n\n# {name}\n', encoding='utf-8')
    return skill_dir

def _write_skill_package(root: Path, name: str, *, allowed_tools: list[str] | None=None, actions: list[dict[str, Any]] | None=None, required_capabilities: list[dict[str, Any]] | None=None, jit_tools: list[dict[str, Any]] | None=None, scripts: dict[str, str] | None=None, extra_resources: dict[str, str] | None=None, body: str | None=None) -> Path:
    skill_dir = root / name
    metadata: dict[str, str] = {'agent-libos.version': 'v0'}
    if actions:
        metadata['agent-libos.actions'] = 'references/agent-libos/actions.json'
    if required_capabilities:
        metadata['agent-libos.required-capabilities'] = 'references/agent-libos/required-capabilities.json'
    if jit_tools:
        metadata['agent-libos.jit-tools'] = 'references/agent-libos/jit-tools.json'
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter_lines = ['---', f'name: {name}', f'description: Adds tools for {name}.', 'allowed-tools:']
    for tool in allowed_tools or []:
        frontmatter_lines.append(f'  - {tool}')
    frontmatter_lines.append('metadata:')
    for key, value in metadata.items():
        frontmatter_lines.append(f'  {key}: {value}')
    frontmatter_lines.append('---')
    selected_body = body or f'# {name}\n\nUse this skill for deterministic checks.\n'
    (skill_dir / 'SKILL.md').write_text('\n'.join(frontmatter_lines) + '\n\n' + selected_body, encoding='utf-8')
    refs = skill_dir / 'references' / 'agent-libos'
    refs.mkdir(parents=True, exist_ok=True)
    if actions:
        (refs / 'actions.json').write_text(json.dumps(actions), encoding='utf-8')
    if required_capabilities:
        (refs / 'required-capabilities.json').write_text(json.dumps(required_capabilities), encoding='utf-8')
    if jit_tools:
        (refs / 'jit-tools.json').write_text(json.dumps(jit_tools), encoding='utf-8')
    for path, content in (scripts or {}).items():
        target = skill_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    for path, content in (extra_resources or {}).items():
        target = skill_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    package = Runtime.open('local')
    try:
        package.skills.validate_package_path(skill_dir)
    finally:
        package.close()
    return skill_dir

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_libos import AgentImage, Runtime
from agent_libos.models import CapabilityRight
from tests.support.fakes import FakeDenoSandbox, RecordingActionClient
from tests.support.skills import write_skill_package


class TestSkillIntegration:

    def test_jit_skill_tool_is_process_local_and_uses_deno_validation_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(Path(temp_dir), 'jit-skill', jit_tools=[{'name': 'skill_count', 'description': 'Count text characters.', 'source_path': 'scripts/skill_count.ts', 'input_schema': {'type': 'object'}, 'output_schema': {'type': 'object'}, 'tests': [{'args': {'text': 'abc'}, 'expected': {'count': 3}}]}], scripts={'scripts/skill_count.ts': 'export function run(args, libos) { /* fake:count_chars */ return {}; }\n'})
            runtime = Runtime.open('local')
            runtime.tools.sandbox = FakeDenoSandbox()
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
            image_skill = write_skill_package(Path(temp_dir), 'image-skill', allowed_tools=['echo'])
            extra_skill = write_skill_package(Path(temp_dir), 'parent-extra', allowed_tools=['read_text_file'])
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
            skill_dir = write_skill_package(Path(temp_dir), 'checkpoint-skill', allowed_tools=['read_text_file'])
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

    def test_loaded_skill_instructions_are_materialized_into_llm_prompt_and_persisted_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(Path(temp_dir), 'prompt-skill', allowed_tools=['echo'], body='Always preserve the phrase skill-instruction-token in planning context.\n', actions=[{'name': 'prompt_action', 'use_cases': ['prompt testing']}])
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
                assert persisted[0].messages['sha256']
                assert 'skill-instruction-token' not in str(persisted[0].messages)
            finally:
                runtime.close()

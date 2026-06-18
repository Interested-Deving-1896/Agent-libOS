from __future__ import annotations
import pytest
import tempfile
from pathlib import Path
from agent_libos import AgentImage, Runtime
from agent_libos.models import CapabilityRight, EventType
from agent_libos.models.exceptions import NotFound, ValidationError
from agent_libos.substrate import LocalResourceProviderSubstrate

class TestImageRegistration:

    def test_register_image_primitive_validates_tools_and_emits_audit(self) -> None:
        runtime = Runtime.open('local')
        try:
            image = AgentImage(image_id='custom-review:v0', name='custom-review', system_prompt='Custom review image.', default_tools=['read_memory_object', 'human_output'], safety_profile='review')
            runtime.register_image(image, actor='cli')
            assert runtime.get_image('custom-review:v0') is image
            assert 'image.register' in [record.action for record in runtime.audit.trace()]
            assert EventType.IMAGE_REGISTERED in [event.type for event in runtime.events.list()]
        finally:
            runtime.close()

    def test_register_image_rejects_unknown_default_tool(self) -> None:
        runtime = Runtime.open('local')
        try:
            with pytest.raises(ValidationError):
                runtime.register_image({'image_id': 'bad-image:v0', 'name': 'bad-image', 'default_tools': ['not_a_real_tool']}, actor='cli')
        finally:
            runtime.close()

    def test_register_image_rejects_invalid_required_capability_right(self) -> None:
        runtime = Runtime.open('local')
        try:
            with pytest.raises(ValidationError):
                runtime.register_image({'image_id': 'bad-right-image:v0', 'name': 'bad-right-image', 'required_capabilities': [{'resource': 'filesystem:workspace:*', 'rights': ['*']}]}, actor='cli')
        finally:
            runtime.close()

    def test_spawn_rejects_unknown_image_instead_of_defaulting_tools(self) -> None:
        runtime = Runtime.open('local')
        try:
            with pytest.raises(NotFound):
                runtime.process.spawn(image='missing-image:v0', goal='should fail')
        finally:
            runtime.close()

    def test_load_image_from_yaml_tool_reads_file_and_registers_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / 'images' / 'yaml-agent.yaml'
            manifest.parent.mkdir()
            manifest.write_text('\nimage:\n  image_id: yaml-agent:v0\n  name: yaml-agent\n  version: v0\n  system_prompt: |\n    YAML registered image.\n    Keep responses concise.\n  default_tools:\n    - human_output\n    - read_memory_object\n  context_policy: evidence_first\n  safety_profile: yaml-test\n  metadata:\n    role: test\n'.lstrip(), encoding='utf-8')
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image='review-agent:v0', goal='load image')
                runtime.filesystem.grant_path(pid, 'images/yaml-agent.yaml', [CapabilityRight.READ], issued_by='test')
                runtime.image_registry.grant_register(pid, issued_by='test')
                result = runtime.tools.call(pid, 'load_image_from_yaml', {'path': 'images/yaml-agent.yaml'})
                assert result.ok, result.error
                assert result.payload['image_id'] == 'yaml-agent:v0'
                image = runtime.get_image('yaml-agent:v0')
                assert image.system_prompt == 'YAML registered image.\nKeep responses concise.\n'
                assert image.default_tools == ['human_output', 'read_memory_object']
                assert image.metadata == {'role': 'test'}
            finally:
                runtime.close()

    def test_load_image_from_yaml_tool_requires_image_write_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / 'yaml-agent.yaml'
            manifest.write_text('\nimage_id: yaml-agent:v0\nname: yaml-agent\ndefault_tools:\n  - human_output\n'.lstrip(), encoding='utf-8')
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image='review-agent:v0', goal='load image without authority')
                runtime.filesystem.grant_path(pid, 'yaml-agent.yaml', [CapabilityRight.READ], issued_by='test')
                result = runtime.tools.call(pid, 'load_image_from_yaml', {'path': 'yaml-agent.yaml'})
                assert not result.ok
                assert 'lacks write on image:yaml-agent:v0' in (result.error or '')
                with pytest.raises(KeyError):
                    runtime.get_image('yaml-agent:v0')
            finally:
                runtime.close()

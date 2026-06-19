from __future__ import annotations
import pytest
import tempfile
from pathlib import Path
from agent_libos import AgentImage, Runtime
from agent_libos.models import CapabilityRight, EventType
from agent_libos.models.exceptions import CapabilityDenied, HumanApprovalRequired, NotFound, ValidationError
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

    def test_load_image_package_tool_reads_workspace_package_and_registers_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            package = _write_image_package(Path(temp_dir) / 'images' / 'package-agent')
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image='review-agent:v0', goal='load image')
                runtime.filesystem.grant_directory(pid, 'images/package-agent', [CapabilityRight.READ], issued_by='test')
                runtime.image_registry.grant_register(pid, issued_by='test')
                result = runtime.tools.call(pid, 'load_image_package', {'path': 'images/package-agent'})
                assert result.ok, result.error
                assert result.payload['image_id'] == 'package-agent:v0'
                assert result.payload['boot_kind'] == 'image_package'
                assert result.payload['package_sha256']
                image = runtime.get_image('package-agent:v0')
                assert image.system_prompt.replace('\r\n', '\n') == 'Package registered image.\nKeep responses concise.\n'
                assert image.default_tools == ['human_output', 'read_memory_object']
                assert image.metadata['role'] == 'test'
                assert image.metadata['package_kind'] == 'image_package'
                assert package.exists()
            finally:
                runtime.close()

    def test_load_image_package_tool_requires_image_write_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_image_package(Path(temp_dir) / 'package-agent')
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image='review-agent:v0', goal='load image without authority')
                runtime.filesystem.grant_directory(pid, 'package-agent', [CapabilityRight.READ], issued_by='test')
                result = runtime.tools.call(pid, 'load_image_package', {'path': 'package-agent'})
                assert not result.ok
                assert 'lacks write on image:package-agent:v0' in (result.error or '')
                with pytest.raises(KeyError):
                    runtime.get_image('package-agent:v0')
            finally:
                runtime.close()

    def test_image_package_workspace_is_private_and_manifest_granted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_image_package(Path(temp_dir) / 'package-agent')
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                runtime.image_registry.register_from_package_path(Path(temp_dir) / 'package-agent', actor='cli')
                first = runtime.process.spawn(image='package-agent:v0', goal='first')
                second = runtime.process.spawn(image='package-agent:v0', goal='second')
                first_cwd = runtime.process.working_directory(first)
                second_cwd = runtime.process.working_directory(second)

                assert first_cwd != second_cwd
                assert runtime.filesystem.read_text(first, 'seed.txt', cwd=first_cwd).content.replace('\r\n', '\n') == 'seed\n'
                runtime.filesystem.write_text(first, 'seed.txt', 'changed\n', cwd=first_cwd)
                assert runtime.filesystem.read_text(first, 'seed.txt', cwd=first_cwd).content.replace('\r\n', '\n') == 'changed\n'
                assert runtime.filesystem.read_text(second, 'seed.txt', cwd=second_cwd).content.replace('\r\n', '\n') == 'seed\n'
            finally:
                runtime.close()

    def test_image_package_without_workspace_grants_cannot_read_materialized_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_image_package(Path(temp_dir) / 'package-agent', workspace_grants=False)
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                runtime.image_registry.register_from_package_path(Path(temp_dir) / 'package-agent', actor='cli')
                pid = runtime.process.spawn(image='package-agent:v0', goal='no grant')
                cwd = runtime.process.working_directory(pid)
                resource = runtime.filesystem.resource_for_path('seed.txt', cwd=cwd)

                assert not runtime.capability.check(pid, resource, CapabilityRight.READ)
                with pytest.raises((CapabilityDenied, HumanApprovalRequired)):
                    runtime.filesystem.read_text(pid, 'seed.txt', cwd=cwd)
            finally:
                runtime.close()

    def test_image_package_jit_tools_are_process_local_and_not_workspace_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_image_package(Path(temp_dir) / 'package-agent', with_jit=True)
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                result = runtime.image_registry.register_from_package_path(Path(temp_dir) / 'package-agent', actor='cli')
                assert result.image.metadata['package_jit_tools'] == ['package_count']
                pid = runtime.process.spawn(image='package-agent:v0', goal='jit')
                visible = {row['name'] for row in runtime.tools.visible_tools(pid)}

                assert 'package_count' in visible
                assert 'package_count' in runtime.process.get(pid).tool_table
                assert not (Path(temp_dir) / runtime.process.working_directory(pid) / 'tools').exists()
            finally:
                runtime.close()


def _write_image_package(root: Path, *, workspace_grants: bool = True, with_jit: bool = False) -> Path:
    root.mkdir(parents=True)
    grants = """
  grants:
    - path: .
      rights: [read, write]
      recursive: true
""".rstrip() if workspace_grants else "  grants: []"
    jit_line = "\njit_tools: tools/jit-tools.json" if with_jit else ""
    root.joinpath('IMAGE.yaml').write_text(f"""
image_id: package-agent:v0
name: package-agent
version: v0
prompt: prompt.md
default_tools:
  - human_output
  - read_memory_object
context_policy: evidence_first
safety_profile: package-test
metadata:
  role: test{jit_line}
workspace:
  source: workspace
  working_directory: .
{grants}
""".lstrip(), encoding='utf-8')
    root.joinpath('prompt.md').write_text('Package registered image.\nKeep responses concise.\n', encoding='utf-8')
    workspace = root / 'workspace'
    workspace.mkdir()
    workspace.joinpath('seed.txt').write_text('seed\n', encoding='utf-8')
    if with_jit:
        scripts = root / 'tools' / 'scripts'
        scripts.mkdir(parents=True)
        root.joinpath('tools', 'jit-tools.json').write_text(
            '[{"name":"package_count","description":"Count text characters.","source_path":"tools/scripts/package_count.ts","input_schema":{"type":"object"},"output_schema":{"type":"object"},"tests":[]}]',
            encoding='utf-8',
        )
        scripts.joinpath('package_count.ts').write_text(
            'export function run(args, libos) { return { count: String(args.text || "").length }; }\n',
            encoding='utf-8',
        )
    return root

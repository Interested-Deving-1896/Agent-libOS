from __future__ import annotations
import pytest
import json
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from agent_libos import Runtime
from agent_libos.models import CapabilityRight, ObjectMetadata, ObjectType, ResourceBudget, ResourceUsage
from agent_libos.models.exceptions import CapabilityDenied, ValidationError

class TestImageCommit:

    def test_commit_requires_checkpoint_read_and_image_write(self) -> None:
        with _runtime() as runtime:
            pid = runtime.process.spawn(image='base-agent:v0', goal='commit source')
            checkpoint_id = runtime.checkpoint.create(pid, 'commit point', actor=pid)
            with pytest.raises(CapabilityDenied):
                runtime.image_registry.commit_from_checkpoint(actor=pid, checkpoint_id=checkpoint_id, image_id='committed-no-write:v0', name='committed-no-write')
            runtime.image_registry.grant_register(pid, 'committed-no-read:v0', issued_by='test')
            other = runtime.process.spawn(image='base-agent:v0', goal='other')
            with pytest.raises(CapabilityDenied):
                runtime.image_registry.commit_from_checkpoint(actor=other, checkpoint_id=checkpoint_id, image_id='committed-no-read:v0', name='committed-no-read')

    def test_committed_image_spawns_baked_memory_without_external_authority(self) -> None:
        with _runtime() as runtime:
            pid = runtime.process.spawn(image='base-agent:v0', goal='learn state')
            runtime.memory.create_object(pid=pid, object_type=ObjectType.ARTIFACT, payload={'learned': 'state'}, metadata=ObjectMetadata(title='Baked state'), name='baked-state', immutable=True)
            runtime.filesystem.grant_path(pid, 'README.md', [CapabilityRight.READ], issued_by='test')
            runtime.capability.grant(pid, 'custom_provider:remote-state', [CapabilityRight.READ], issued_by='test')
            checkpoint_id = runtime.checkpoint.create(pid, 'state ready', actor=pid)
            runtime.image_registry.grant_register(pid, 'stateful-agent:v0', issued_by='test')
            result = runtime.image_registry.commit_from_checkpoint(actor=pid, checkpoint_id=checkpoint_id, image_id='stateful-agent:v0', name='stateful-agent')
            assert result.image.boot['kind'] == 'checkpoint_commit'
            assert result.image.required_capabilities
            required_resources = {item['resource'] for item in result.image.required_capabilities}
            assert 'filesystem:workspace:README.md' in required_resources
            assert 'custom_provider:remote-state' in required_resources
            child = runtime.process.spawn(image='stateful-agent:v0', goal='use baked state')
            baked = runtime.memory.get_object_by_name(child, 'baked-state')
            assert baked.payload == {'learned': 'state'}
            assert not runtime.capability.check(child, 'filesystem:workspace:README.md', CapabilityRight.READ)
            assert not runtime.capability.check(child, 'custom_provider:remote-state', CapabilityRight.READ)
            assert 'image.required_capabilities_declared_only' in [record.action for record in runtime.audit.trace()]

    def test_exec_into_committed_image_restores_baked_memory_without_granting_required_caps(self) -> None:
        with _runtime() as runtime:
            source = runtime.process.spawn(image='base-agent:v0', goal='source')
            runtime.memory.create_object(pid=source, object_type=ObjectType.ARTIFACT, payload={'role': 'committed'}, metadata=ObjectMetadata(title='Role'), name='role', immutable=True)
            runtime.capability.grant(source, 'shell:python', [CapabilityRight.EXECUTE], issued_by='test')
            checkpoint_id = runtime.checkpoint.create(source, 'before commit', actor=source)
            runtime.image_registry.grant_register(source, 'exec-state:v0', issued_by='test')
            runtime.image_registry.commit_from_checkpoint(actor=source, checkpoint_id=checkpoint_id, image_id='exec-state:v0', name='exec-state')
            target = runtime.process.spawn(image='base-agent:v0', goal='target')
            runtime.exec_process(target, 'exec-state:v0', goal='new goal', preserve_capabilities=False)
            assert runtime.memory.get_object_by_name(target, 'role').payload == {'role': 'committed'}
            assert not runtime.capability.check(target, 'shell:python', CapabilityRight.EXECUTE)

    def test_committed_image_does_not_save_or_restore_resource_state(self) -> None:
        with _runtime() as runtime:
            source = runtime.process.spawn(
                image='base-agent:v0',
                goal='source',
                resource_budget=ResourceBudget(max_tool_calls=5, max_llm_total_tokens=20),
            )
            runtime.resources.charge(source, ResourceUsage(tool_calls=2, llm_total_tokens=7), source='test')
            checkpoint_id = runtime.checkpoint.create(source, 'resource state', actor=source)
            runtime.image_registry.grant_register(source, 'resource-state:v0', issued_by='test')
            result = runtime.image_registry.commit_from_checkpoint(actor=source, checkpoint_id=checkpoint_id, image_id='resource-state:v0', name='resource-state')

            found = runtime.store.get_image_artifact(result.image.boot['artifact_id'])
            assert found is not None
            artifact, _metadata = found
            assert 'resource_budget_json' not in artifact['source_process']
            assert 'resource_usage_json' not in artifact['source_process']

            spawned = runtime.process.spawn(
                image='resource-state:v0',
                goal='spawned',
                resource_budget=ResourceBudget(max_tool_calls=3, max_llm_total_tokens=30),
            )
            process = runtime.process.get(spawned)

            assert process.resource_budget.max_tool_calls == 3
            assert process.resource_budget.max_llm_total_tokens == 30
            assert process.resource_usage.tool_calls == 0
            assert process.resource_usage.llm_total_tokens == 0

    def test_duplicate_commit_requires_replace(self) -> None:
        with _runtime() as runtime:
            pid = runtime.process.spawn(image='base-agent:v0', goal='source')
            checkpoint_id = runtime.checkpoint.create(pid, 'commit', actor=pid)
            runtime.image_registry.grant_register(pid, 'dupe:v0', issued_by='test')
            runtime.image_registry.commit_from_checkpoint(actor=pid, checkpoint_id=checkpoint_id, image_id='dupe:v0', name='dupe')
            with pytest.raises(ValidationError):
                runtime.image_registry.commit_from_checkpoint(actor=pid, checkpoint_id=checkpoint_id, image_id='dupe:v0', name='dupe')

    def test_cli_images_commit_list_and_inspect(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            db_path = str(Path(temp_dir) / 'runtime.sqlite')
            spawned = _run_cli_json(['--db', db_path, 'spawn', '--goal', 'source'])
            created = _run_cli_json(['--db', db_path, 'checkpoint', 'create', spawned['pid'], 'commit'])
            committed = _run_cli_json(['--db', db_path, 'images', 'commit', created['checkpoint_id'], 'cli-committed:v0', '--name', 'cli-committed'])
            listed = _run_cli_json(['--db', db_path, 'images', 'list'])
            inspected = _run_cli_json(['--db', db_path, 'images', 'inspect', 'cli-committed:v0'])
            assert committed['boot']['kind'] == 'checkpoint_commit'
            assert 'cli-committed:v0' in {item['image_id'] for item in listed}
            assert inspected['image']['boot']['kind'] == 'checkpoint_commit'

def _run_cli_json(args: list[str]) -> object:
    result = subprocess.run([sys.executable, '-m', 'agent_libos.api.cli', *args], cwd=Path.cwd(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return json.loads(result.stdout)

@contextmanager
def _runtime() -> Iterator[Runtime]:
    runtime = Runtime.open(':memory:')
    try:
        yield runtime
    finally:
        runtime.shutdown(actor='test', reason='test complete')

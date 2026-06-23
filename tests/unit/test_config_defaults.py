from __future__ import annotations
import pytest
import asyncio
import json
from dataclasses import replace
from pydantic import ValidationError as PydanticValidationError

from agent_libos.config import AgentLibOSConfig, DEFAULT_CONFIG, LLMDefaults, LLMProfile, RuntimeDefaults
from agent_libos.llm.client import LLMCompletion
from agent_libos.models.exceptions import HumanResponseRequired, ValidationError
from agent_libos.models import CapabilityRight, ProcessStatus
from agent_libos.runtime.runtime import Runtime
from agent_libos.storage import SQLiteStore

class TestConfigDefaults:

    def test_llm_profiles_validate_default_profile_reference(self) -> None:
        config = AgentLibOSConfig(
            llm=LLMDefaults(
                default_profile_id="coding",
                profiles={
                    "default": LLMProfile(),
                    "coding": LLMProfile(model="coding-model", temperature=0.0, max_tokens=256),
                },
            )
        )
        assert config.llm.default_profile_id == "coding"
        assert config.llm.profiles["coding"].model == "coding-model"

        with pytest.raises(ValueError, match="default_profile_id"):
            AgentLibOSConfig(llm=LLMDefaults(default_profile_id="missing", profiles={"default": LLMProfile()}))

    def test_runtime_default_run_until_idle_is_unbounded(self) -> None:
        runtime = Runtime(SQLiteStore(':memory:'), llm_client=ScriptedActionClient())
        try:
            pid = runtime.process.spawn(image=DEFAULT_CONFIG.runtime.default_image_id, goal='two-step process')
            results = asyncio.run(runtime.arun_until_idle())
            assert len(results) == 2
            assert results[0]['action']['action'] == 'create_memory_object'
            assert results[1]['action']['action'] == 'process_exit'
            assert runtime.process.get(pid).status == ProcessStatus.EXITED
        finally:
            runtime.close()

    def test_runtime_uses_configured_default_quanta_when_present(self) -> None:
        config = AgentLibOSConfig(runtime=RuntimeDefaults(run_until_idle_max_quanta=1))
        runtime = Runtime(SQLiteStore(':memory:'), llm_client=ScriptedActionClient(), config=config)
        try:
            pid = runtime.process.spawn(image=config.runtime.default_image_id, goal='two-step process')
            results = asyncio.run(runtime.arun_until_idle())
            assert len(results) == 1
            assert results[0]['action']['action'] == 'create_memory_object'
            assert runtime.process.get(pid).status == ProcessStatus.RUNNABLE
        finally:
            runtime.close()

    def test_runtime_respects_explicit_quanta_budget(self) -> None:
        config = AgentLibOSConfig(runtime=RuntimeDefaults(run_until_idle_max_quanta=1))
        runtime = Runtime(SQLiteStore(':memory:'), llm_client=ScriptedActionClient(), config=config)
        try:
            pid = runtime.process.spawn(image=config.runtime.default_image_id, goal='two-step process')
            results = asyncio.run(runtime.arun_until_idle(max_quanta=1))
            assert len(results) == 1
            assert results[0]['action']['action'] == 'create_memory_object'
            assert runtime.process.get(pid).status == ProcessStatus.RUNNABLE
        finally:
            runtime.close()

    def test_process_until_idle_uses_configured_default_quanta_when_present(self) -> None:
        config = AgentLibOSConfig(runtime=RuntimeDefaults(run_until_idle_max_quanta=1))
        runtime = Runtime(SQLiteStore(':memory:'), llm_client=ScriptedActionClient(), config=config)
        try:
            pid = runtime.process.spawn(image=config.runtime.default_image_id, goal='two-step process')
            results = asyncio.run(runtime.arun_process_until_idle(pid))
            assert len(results) == 1
            assert results[0]['action']['action'] == 'create_memory_object'
            assert runtime.process.get(pid).status == ProcessStatus.RUNNABLE
        finally:
            runtime.close()

    def test_default_images_are_built_from_runtime_config(self) -> None:
        config = AgentLibOSConfig(runtime=RuntimeDefaults(workspace_namespace='repo'))
        runtime = Runtime(SQLiteStore(':memory:'), llm_client=ScriptedActionClient(), config=config)
        try:
            pid = runtime.process.spawn(image=config.runtime.coding_image_id, goal='inspect')
            assert runtime.capability.permission_policy(pid, 'filesystem:repo:*', CapabilityRight.READ) == runtime.capability.ALWAYS_ALLOW
            assert runtime.capability.permission_policy(pid, DEFAULT_CONFIG.runtime.default_human_resource, CapabilityRight.WRITE) == runtime.capability.ALWAYS_ALLOW
        finally:
            runtime.close()

    def test_runtime_open_uses_configured_local_store_target_when_target_is_omitted(self) -> None:
        config = AgentLibOSConfig(runtime=RuntimeDefaults(local_store_target='ephemeral'))
        runtime = Runtime.open(config=config)
        try:
            assert runtime.store.path == ':memory:'
        finally:
            runtime.close()

    def test_spawn_without_image_uses_configured_default_image(self) -> None:
        config = AgentLibOSConfig(
            runtime=RuntimeDefaults(default_image_id='custom-base:v0', coding_image_id='custom-coding:v0')
        )
        runtime = Runtime.open(config=config)
        try:
            pid = runtime.process.spawn(goal='custom image')
            assert runtime.process.get(pid).image_id == 'custom-base:v0'
            assert 'custom-base:v0' in runtime.images
            assert 'custom-coding:v0' in runtime.images
        finally:
            runtime.close()

    def test_config_validation_rejects_invalid_numeric_bounds(self) -> None:
        with pytest.raises(PydanticValidationError, match='fork_budget_divisor'):
            AgentLibOSConfig(runtime=RuntimeDefaults(), process=replace(DEFAULT_CONFIG.process, fork_budget_divisor=0))
        with pytest.raises(PydanticValidationError, match='object_task_wait_max_timeout_s'):
            AgentLibOSConfig(gui=replace(DEFAULT_CONFIG.gui, object_task_wait_default_timeout_s=5, object_task_wait_max_timeout_s=4))
        with pytest.raises(PydanticValidationError, match='trusted_issuer_prefixes'):
            AgentLibOSConfig(capability=replace(DEFAULT_CONFIG.capability, trusted_issuer_prefixes=('',)))

    def test_sqlite_store_llm_call_limits_use_runtime_config(self) -> None:
        config = AgentLibOSConfig(llm=LLMDefaults(call_record_list_limit=1, call_record_hard_limit=2))
        runtime = Runtime.open(config=config)
        try:
            assert runtime.store.list_llm_calls() == []
            with pytest.raises(ValidationError, match='hard cap 2'):
                runtime.store.list_llm_calls(limit=3)
        finally:
            runtime.close()

    def test_static_tool_specs_are_generated_from_runtime_config(self) -> None:
        config = AgentLibOSConfig(
            tools=replace(DEFAULT_CONFIG.tools, shell_timeout_s=6.0, standard_timeout_s=2.5),
            shell=replace(DEFAULT_CONFIG.shell, timeout_hard_limit_s=7.0, max_stdout_chars=9, stdout_hard_limit_chars=11),
        )
        runtime = Runtime.open(config=config)
        try:
            row = next(item for item in runtime.tools.list() if item['name'] == 'read_text_file')
            spec = json.loads(row['spec_json'])
            props = spec['input_schema']['properties']
            assert props['max_bytes']['default'] == config.tools.filesystem_read_max_bytes
            assert props['max_bytes']['maximum'] == config.tools.filesystem_read_hard_limit_bytes
            assert spec['policy']['timeout_s'] == 2.5
        finally:
            runtime.close()

    def test_runtime_tool_argument_defaults_use_runtime_config(self) -> None:
        config = AgentLibOSConfig(runtime=RuntimeDefaults(default_human='admin'))
        runtime = Runtime.open(config=config)
        try:
            pid = runtime.process.spawn(goal='configured human default')
            runtime.capability.grant(pid, 'human:admin', [CapabilityRight.WRITE], issued_by='test')

            with pytest.raises(HumanResponseRequired):
                runtime.tools.call(
                    pid,
                    'request_permission',
                    {'resource': 'filesystem:workspace:configured.txt', 'rights': ['write'], 'reason': 'test default'},
                )

            pending = runtime.human.pending()
            assert len(pending) == 1
            assert pending[0].human == 'admin'

            output_pid = runtime.process.spawn(goal='configured human output')
            runtime.capability.grant(output_pid, 'human:admin', [CapabilityRight.WRITE], issued_by='test')
            output = runtime.tools.call(output_pid, 'human_output', {'message': 'hello admin'})
            assert output.ok
            output_request = runtime.human.list(pid=output_pid)[0]
            assert output_request.human == 'admin'
        finally:
            runtime.close()

class ScriptedActionClient:

    def __init__(self) -> None:
        self.actions = [{'action': 'create_memory_object', 'type': 'observation', 'name': 'step', 'payload': {'ok': True}}, {'action': 'process_exit', 'payload': {'done': True}}]

    def complete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        action = self.actions.pop(0)
        name = str(action['action'])
        args = {key: value for key, value in action.items() if key != 'action'}
        return LLMCompletion(content='', tool_calls=[{'id': f'config_{len(self.actions)}', 'name': name, 'arguments': json.dumps(args)}])

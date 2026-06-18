from __future__ import annotations
import pytest
import asyncio
import json
from agent_libos.config import AgentLibOSConfig, DEFAULT_CONFIG, RuntimeDefaults
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import CapabilityRight, ProcessStatus
from agent_libos.runtime.runtime import Runtime
from agent_libos.storage import SQLiteStore

class TestConfigDefaults:

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

class ScriptedActionClient:

    def __init__(self) -> None:
        self.actions = [{'action': 'create_memory_object', 'type': 'observation', 'name': 'step', 'payload': {'ok': True}}, {'action': 'process_exit', 'payload': {'done': True}}]

    def complete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        action = self.actions.pop(0)
        name = str(action['action'])
        args = {key: value for key, value in action.items() if key != 'action'}
        return LLMCompletion(content='', tool_calls=[{'id': f'config_{len(self.actions)}', 'name': name, 'arguments': json.dumps(args)}])

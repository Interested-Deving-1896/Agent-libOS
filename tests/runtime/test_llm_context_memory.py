from __future__ import annotations
import pytest
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from agent_libos import Runtime
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.llm.client import LLMCompletion
from agent_libos.llm.context_memory import context_object_name
from agent_libos.models import (
    AgentImage,
    CapabilityRight,
    EventType,
    JIT_MULTIPLEXER_TOOL_NAME,
    JIT_TOOL_EXPOSURE_MULTIPLEXED,
    ObjectPatch,
    ObjectRight,
    ObjectType,
    PROMPT_MODE_LIBOS_DEFAULT,
    ProcessStatus,
    ResourceBudget,
    ViewMode,
)
from tests.support.fakes import FakeDenoSandbox, RecordingActionClient
from tests.support.skills import write_skill_package

class TestLLMContextMemory:

    def test_llm_context_is_process_readable_writable_memory_object(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.llm.client = RecordingActionClient([{'action': 'create_memory_object', 'type': 'observation', 'payload': {'seen': 1}}])
            pid = runtime.process.spawn(image='base-agent:v0', goal='create context')
            runtime.run_next_process_once()
            name = context_object_name(pid)
            obj = runtime.store.get_object_by_name(name, namespace=runtime.memory.resolve_namespace(pid))
            assert obj is not None
            assert obj is not None
            assert not obj.immutable
            assert obj.payload['kind'] == 'llm_context'
            assert runtime.capability.check(pid, f'object:{obj.oid}', ObjectRight.READ)
            assert runtime.capability.check(pid, f'object:{obj.oid}', ObjectRight.WRITE)
            process = runtime.process.get(pid)
            assert obj.oid in [handle.oid for handle in process.memory_view.roots]
            read = runtime.tools.call(pid, 'read_memory_object', {'name': name})
            appended = runtime.tools.call(pid, 'append_memory_object', {'name': name, 'entry': {'kind': 'agent_note', 'text': 'keep this in context'}})
            updated = runtime.store.get_object_by_name(name, namespace=runtime.memory.resolve_namespace(pid))
            assert read.ok, read.error
            assert appended.ok, appended.error
            assert updated.payload['entries'][-1]['kind'] == 'agent_note'
        finally:
            runtime.close()

    def test_llm_context_prompt_grows_by_appending_to_preserve_cache_prefix(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.llm.client = RecordingActionClient([{'action': 'create_memory_object', 'type': 'observation', 'payload': {'step': 1}}, {'action': 'create_memory_object', 'type': 'observation', 'payload': {'step': 2}}])
            pid = runtime.process.spawn(image='base-agent:v0', goal='append context')
            runtime.run_next_process_once()
            runtime.run_next_process_once()
            first, second = runtime.llm.client.user_prompts
            assert 'Cache strategy: append_only_stable_prefix' in first
            assert 'LLM context object' in first
            assert second.startswith(first)
            context = runtime.store.get_object_by_name(context_object_name(pid), namespace=runtime.memory.resolve_namespace(pid))
            kinds = [entry['kind'] for entry in context.payload['entries']]
            assert 'memory_delta' in kinds
            assert len(second) > len(first)
        finally:
            runtime.close()

    def test_llm_context_rendered_prompt_is_charged_to_materialization_budget_before_model_call(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.llm.client = RecordingActionClient([{'action': 'process_exit', 'payload': {'should_not_run': True}}])
            pid = runtime.process.spawn(
                image='base-agent:v0',
                goal='budget context before model call',
                resource_budget=ResourceBudget(
                    max_context_materialization_tokens=100_000,
                    max_context_materialization_total_tokens=1,
                ),
            )

            result = runtime.run_next_process_once()

            assert result['resource_limit_exceeded']
            assert runtime.llm.client.tool_batches == []
            assert runtime.process.get(pid).status == ProcessStatus.KILLED
        finally:
            runtime.close()

    def test_llm_prompt_lists_only_process_visible_tools(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.llm.client = RecordingActionClient([{'action': 'process_exit', 'payload': {'done': True}}])
            pid = runtime.process.spawn(image='base-agent:v0', goal='exit')
            runtime.run_next_process_once()
            tool_names = {tool['function']['name'] for tool in runtime.llm.client.tool_batches[0]}
            assert 'process_exit' in tool_names
            assert 'read_text_file' not in tool_names
            assert 'read_text_file' not in runtime.llm.client.user_prompts[0]
        finally:
            runtime.close()

    def test_multiplexed_jit_tool_call_dispatches_real_jit_tool(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.tools.sandbox = FakeDenoSandbox()
            _register_multiplexed_image(runtime)
            pid = runtime.process.spawn(image='multiplexed-jit:v0', goal='count with a JIT tool')
            _register_count_tool(runtime, pid, 'count_chars')
            runtime.llm.client = RecordingActionClient([
                {
                    'action': JIT_MULTIPLEXER_TOOL_NAME,
                    'tool_name': 'count_chars',
                    'arguments': {'text': 'hello'},
                }
            ])

            result = runtime.run_next_process_once()
            tool_names = {tool['function']['name'] for tool in runtime.llm.client.tool_batches[0]}

            assert result['ok'], result
            assert result['action']['action'] == 'count_chars'
            assert result['result']['payload'] == {'count': 5}
            assert JIT_MULTIPLEXER_TOOL_NAME in tool_names
            assert 'count_chars' not in tool_names
        finally:
            runtime.close()

    def test_multiplexed_jit_direct_name_and_bad_args_are_repairable(self) -> None:
        config = replace(DEFAULT_CONFIG, llm=replace(DEFAULT_CONFIG.llm, action_repair_attempts=3))
        runtime = Runtime.open('local', config=config)
        try:
            runtime.tools.sandbox = FakeDenoSandbox()
            _register_multiplexed_image(runtime)
            pid = runtime.process.spawn(image='multiplexed-jit:v0', goal='repair bad JIT action')
            _register_count_tool(
                runtime,
                pid,
                'strict_count',
                input_schema={
                    'type': 'object',
                    'properties': {'text': {'type': 'string'}},
                    'required': ['text'],
                    'additionalProperties': False,
                },
            )
            runtime.llm.client = RecordingActionClient([
                {'action': 'strict_count', 'text': 'hello'},
                {
                    'action': JIT_MULTIPLEXER_TOOL_NAME,
                    'tool_name': 'strict_count',
                    'arguments': {'extra': 'rejected'},
                },
                {'action': 'process_exit', 'payload': {'done': True}},
            ])

            result = runtime.run_next_process_once()

            assert result['ok'], result
            assert result['action']['action'] == 'process_exit'
            repairs = [record for record in runtime.audit.trace() if record.action == 'llm.action_repair_requested']
            assert any('strict_count' in str(record.decision) for record in repairs)
            assert any('Additional properties' in str(record.decision) for record in repairs)
            assert not any(
                record.action == 'tool.call' and record.decision.get('tool') == 'strict_count'
                for record in runtime.audit.trace()
            )
        finally:
            runtime.close()

    def test_multiplexed_prompt_context_hides_jit_catalog(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.tools.sandbox = FakeDenoSandbox()
            _register_multiplexed_image(runtime, prompt_mode=PROMPT_MODE_LIBOS_DEFAULT)
            pid = runtime.process.spawn(image='multiplexed-jit:v0', goal='hide catalog')
            _register_count_tool(runtime, pid, 'secret_count')
            runtime.events.emit(
                EventType.TOOL_COMPLETED,
                source='tool:secret_count',
                target=pid,
                payload={'secret_count': {'action': 'secret_count'}},
            )
            runtime.llm.client = RecordingActionClient([{'action': 'process_exit', 'payload': {'done': True}}])

            runtime.run_next_process_once()

            prompt = runtime.llm.client.user_prompts[0]
            tool_names = {tool['function']['name'] for tool in runtime.llm.client.tool_batches[0]}
            assert JIT_MULTIPLEXER_TOOL_NAME in prompt
            assert 'secret_count' not in prompt
            assert JIT_MULTIPLEXER_TOOL_NAME in tool_names
            assert 'secret_count' not in tool_names
            assert JIT_MULTIPLEXER_TOOL_NAME in runtime.tools.model_tool_names(pid)
            assert 'secret_count' not in runtime.tools.model_tool_names(pid)
        finally:
            runtime.close()

    def test_multiplexed_prompt_context_hides_skill_jit_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(
                Path(temp_dir),
                'secret-jit-skill',
                jit_tools=[
                    {
                        'name': 'skill_secret_count',
                        'description': 'Count text characters.',
                        'source_path': 'scripts/skill_secret_count.ts',
                        'input_schema': {'type': 'object'},
                        'output_schema': {'type': 'object'},
                        'tests': [{'args': {'text': 'abc'}, 'expected': {'count': 3}}],
                    }
                ],
                scripts={
                    'scripts/skill_secret_count.ts': (
                        'export function run(args, libos) { /* fake:count_chars */ return {}; }\n'
                    )
                },
                body='Use this skill without relying on an automatic JIT catalog.\n',
            )
            runtime = Runtime.open('local')
            try:
                runtime.tools.sandbox = FakeDenoSandbox()
                _register_multiplexed_image(runtime, prompt_mode=PROMPT_MODE_LIBOS_DEFAULT)
                pid = runtime.process.spawn(image='multiplexed-jit:v0', goal='hide skill catalog')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                runtime.capability.grant(pid, 'skill:secret-jit-skill', [CapabilityRight.EXECUTE], issued_by='test')
                runtime.skills.activate_skill(pid, 'secret-jit-skill', actor=pid)
                runtime.llm.client = RecordingActionClient([{'action': 'process_exit', 'payload': {'done': True}}])

                runtime.run_next_process_once()

                prompt = runtime.llm.client.user_prompts[0]
                assert JIT_MULTIPLEXER_TOOL_NAME in prompt
                assert 'skill_secret_count' not in prompt
                assert 'tool_secret' not in prompt
                assert 'secret-jit-skill' in prompt
            finally:
                runtime.close()

    def test_llm_context_appends_updated_object_version(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.llm.client = RecordingActionClient([
                {'action': 'create_memory_object', 'type': 'observation', 'payload': {'step': 1}},
                {'action': 'process_exit', 'payload': {'done': True}},
            ])
            pid = runtime.process.spawn(image='base-agent:v0', goal='track object updates')
            handle = runtime.memory.create_object(
                pid=pid,
                object_type=ObjectType.OBSERVATION,
                payload={'value': 'old-object-token'},
                immutable=False,
                name='changing-observation',
            )
            process = runtime.process.get(pid)
            process.memory_view = runtime.memory.create_view(pid, [handle], mode=ViewMode.READ_ONLY)
            runtime.store.update_process(process)

            runtime.run_next_process_once()
            runtime.memory.update_object(pid, handle, ObjectPatch(payload={'value': 'new-object-token'}))
            runtime.run_next_process_once()

            first, second = runtime.llm.client.user_prompts
            assert 'old-object-token' in first
            assert 'new-object-token' in second
            assert second.startswith(first)
        finally:
            runtime.close()

    def test_llm_executor_fails_closed_when_process_image_is_missing(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.llm.client = ExplodingClient()
            pid = runtime.process.spawn(image='base-agent:v0', goal='missing image')
            process = runtime.process.get(pid)
            process.image_id = 'missing-image:v0'
            runtime.store.update_process(process)

            result = runtime.run_process_once(pid)

            assert not result['ok']
            assert 'agent image not found' in result['error']
            assert runtime.process.get(pid).status == ProcessStatus.FAILED
            assert 'llm.image_missing' in [record.action for record in runtime.audit.trace()]
        finally:
            runtime.close()

    def test_llm_retries_malformed_empty_tool_name_once(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.llm.client = RecordingActionClient([{'action': '', 'path': '.'}, {'action': 'process_exit', 'payload': {'done': True}}])
            pid = runtime.process.spawn(image='base-agent:v0', goal='recover malformed action')
            result = runtime.run_next_process_once()
            assert result['ok']
            assert result['action']['action'] == 'process_exit'
            assert len(runtime.llm.client.user_prompts) == 2
            assert 'could not be dispatched' in runtime.llm.client.user_prompts[1]
            repairs = [record for record in runtime.audit.trace() if record.action == 'llm.action_repair_requested']
            assert len(repairs) == 1
            assert repairs[0].decision is not None
            assert repairs[0].decision['tool_calls_preview'][0]['name'] == ''
            assert '"path"' in repairs[0].decision['tool_calls_preview'][0]['arguments_preview']
        finally:
            runtime.close()

    def test_llm_call_records_persist_sanitized_prompt_output_usage_and_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = f'{temp_dir}/runtime.sqlite'
            runtime = Runtime.open(db)
            try:
                runtime.llm.client = MetadataActionClient()
                pid = runtime.process.spawn(image='base-agent:v0', goal='persist llm calls')
                runtime.run_next_process_once()
                calls = runtime.store.list_llm_calls(pid)
                assert len(calls) == 1
                call = calls[0]
                assert call.pid == pid
                assert call.purpose == 'action_selection'
                assert call.status == 'ok'
                assert call.api == 'chat'
                assert call.model == 'test-model'
                assert call.request_id == 'req_123'
                assert call.response_id == 'resp_123'
                assert call.response_content == 'visible assistant text'
                assert call.usage['total_tokens'] == 17
                assert call.messages['sha256']
                assert call.tools['sha256']
                assert call.tool_calls['sha256']
                assert call.raw_response['sha256']
                assert call.reasoning['sha256']
                assert call.observability['response_content']['sha256']
                serialized = json.dumps(call.__dict__, sort_keys=True)
                assert 'persist llm calls' not in serialized
                assert '"payload": {"done": true}' not in serialized
            finally:
                runtime.close()
            reopened = Runtime.open(db)
            try:
                persisted = reopened.store.list_llm_calls()
                assert len(persisted) == 1
                assert persisted[0].usage['prompt_tokens'] == 13
                assert persisted[0].observability['messages']['bytes'] > 0
            finally:
                reopened.close()

    def test_llm_call_records_can_persist_full_io_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = f'{temp_dir}/runtime.sqlite'
            config = replace(DEFAULT_CONFIG, llm=replace(DEFAULT_CONFIG.llm, persist_full_io=True))
            runtime = Runtime.open(db, config=config)
            try:
                runtime.llm.client = MetadataActionClient()
                pid = runtime.process.spawn(image='base-agent:v0', goal='persist full llm calls')

                runtime.run_next_process_once()
                call = runtime.store.list_llm_calls(pid)[0]

                assert call.messages[1]['content']
                assert 'persist full llm calls' in call.messages[1]['content']
                assert any((tool['function']['name'] == 'process_exit' for tool in call.tools))
                assert call.response_content == 'visible assistant text'
                assert call.tool_calls[0]['name'] == 'process_exit'
                assert call.tool_calls[0]['arguments'] == json.dumps({'payload': {'done': True}})
                assert call.reasoning == {'summary': 'selected process_exit'}
                assert call.raw_response['id'] == 'raw_resp'
                assert call.observability['messages']['sha256']
            finally:
                runtime.close()

            reopened = Runtime.open(db, config=config)
            try:
                persisted = reopened.store.list_llm_calls(pid)[0]
                assert 'persist full llm calls' in persisted.messages[1]['content']
                assert persisted.raw_response['provider'] == 'fake'
            finally:
                reopened.close()

    def test_pending_human_llm_action_survives_runtime_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = f'{temp_dir}/runtime.sqlite'
            path = 'agent_outputs/pending_llm_action_reopen.txt'
            runtime = Runtime.open(db)
            try:
                runtime.llm.client = RecordingActionClient([
                    {'action': 'write_text_file', 'path': path, 'content': 'persisted approval action'},
                ])
                pid = runtime.process.spawn(image='review-agent:v0', goal='write after approval')
                runtime.capability.set_permission_policy(
                    subject=pid,
                    resource=runtime.filesystem.resource_for(path),
                    rights=[CapabilityRight.WRITE],
                    policy='ask_each_time',
                    issued_by='test',
                )
                waiting = runtime.run_next_process_once()
                assert waiting['waiting_human']
                assert runtime.store.get_llm_pending_action(pid)['wait_type'] == 'human'
            finally:
                runtime.close()

            reopened = Runtime.open(db)
            try:
                reopened.llm.client = ExplodingClient()
                reopened.human.drain_terminal_queue(auto_approve=True)
                resumed = reopened.run_next_process_once()

                assert resumed['resumed_after_human']
                assert resumed['action']['action'] == 'write_text_file'
                assert resumed['result']['ok']
                assert (reopened.workspace_root / path).read_text(encoding='utf-8') == 'persisted approval action'
                assert reopened.store.get_llm_pending_action(pid)['status'] == 'completed'
            finally:
                reopened.close()

def _register_multiplexed_image(
    runtime: Runtime,
    *,
    prompt_mode: str | None = None,
) -> None:
    runtime.register_image(
        AgentImage(
            image_id='multiplexed-jit:v0',
            name='multiplexed-jit',
            system_prompt='Use run_jit_tool for JIT tools.',
            prompt_mode=prompt_mode or 'image_only',
            default_tools=['process_exit'],
            jit_tool_exposure=JIT_TOOL_EXPOSURE_MULTIPLEXED,
        ),
        actor='test',
    )


def _register_count_tool(
    runtime: Runtime,
    pid: str,
    name: str,
    *,
    input_schema: dict[str, Any] | None = None,
) -> None:
    candidate = runtime.tools.propose(
        pid,
        {
            'name': name,
            'description': 'Count characters in text.',
            'input_schema': input_schema
            or {'type': 'object', 'properties': {'text': {'type': 'string'}}},
            'output_schema': {'type': 'object'},
        },
        source_code='export function run(args, libos) { /* fake:count_chars */ return {}; }',
        tests=[{'args': {'text': 'abc'}, 'expected': {'count': 3}}],
    )
    assert runtime.tools.validate(candidate).ok
    runtime.tools.register(pid, candidate)


class MetadataActionClient:

    def complete_action(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMCompletion:
        return LLMCompletion(content='visible assistant text', tool_calls=[{'id': 'tool_123', 'name': 'process_exit', 'arguments': json.dumps({'payload': {'done': True}})}], raw=SimpleNamespace(id='raw_resp', provider='fake'), api='chat', response_id='resp_123', request_id='req_123', model='test-model', usage={'prompt_tokens': 13, 'completion_tokens': 4, 'total_tokens': 17}, reasoning={'summary': 'selected process_exit'})


class ExplodingClient:

    def complete_action(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMCompletion:
        raise AssertionError('LLM client should not be called when the process image is missing')

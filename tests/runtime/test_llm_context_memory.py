from __future__ import annotations
import pytest
import json
import tempfile
from types import SimpleNamespace
from agent_libos import Runtime
from agent_libos.llm.client import LLMCompletion
from agent_libos.llm.context_memory import context_object_name
from agent_libos.models import ObjectRight
from tests.support.fakes import RecordingActionClient

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

    def test_llm_call_records_persist_prompt_output_usage_and_reasoning(self) -> None:
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
                assert call.reasoning == {'summary': 'selected process_exit'}
                assert call.raw_response['id'] == 'raw_resp'
                assert call.tool_calls[0]['name'] == 'process_exit'
                assert 'persist llm calls' in call.messages[1]['content']
                assert any((tool['function']['name'] == 'process_exit' for tool in call.tools))
            finally:
                runtime.close()
            reopened = Runtime.open(db)
            try:
                persisted = reopened.store.list_llm_calls()
                assert len(persisted) == 1
                assert persisted[0].usage['prompt_tokens'] == 13
                assert persisted[0].reasoning == {'summary': 'selected process_exit'}
            finally:
                reopened.close()

class MetadataActionClient:

    def complete_action(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMCompletion:
        return LLMCompletion(content='visible assistant text', tool_calls=[{'id': 'tool_123', 'name': 'process_exit', 'arguments': json.dumps({'payload': {'done': True}})}], raw=SimpleNamespace(id='raw_resp', provider='fake'), api='chat', response_id='resp_123', request_id='req_123', model='test-model', usage={'prompt_tokens': 13, 'completion_tokens': 4, 'total_tokens': 17}, reasoning={'summary': 'selected process_exit'})

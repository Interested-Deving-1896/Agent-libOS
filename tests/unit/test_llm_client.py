from __future__ import annotations
import pytest
import asyncio
from types import SimpleNamespace
from typing import Any
from agent_libos.llm.client import LLMClient

class TestLLMClient:

    def test_responses_action_request_converts_chat_tools_and_parses_function_calls(self) -> None:
        response = SimpleNamespace(id='resp_123', model='gpt-test', usage=SimpleNamespace(input_tokens=11, output_tokens=3, total_tokens=14), output_text='', output=[SimpleNamespace(type='reasoning', summary=[{'text': 'choose write_text_file'}]), SimpleNamespace(type='function_call', id='fc_1', call_id='call_1', name='write_text_file', arguments='{"path":"out.txt","content":"ok"}')])
        fake = FakeAsyncOpenAI(responses=FakeResponses(response))
        client = LLMClient(model='gpt-test', api_key='key', api_mode='responses')
        client._async_client = fake
        completion = asyncio.run(client.acomplete_action(messages=[{'role': 'system', 'content': 'system rules'}, {'role': 'user', 'content': 'write a file'}], tools=[{'type': 'function', 'function': {'name': 'write_text_file', 'description': 'Write text.', 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}}]))
        payload = fake.responses.payloads[0]
        assert payload['instructions'] == 'system rules'
        assert payload['input'] == [{'role': 'user', 'content': 'write a file'}]
        assert payload['tools'][0]['name'] == 'write_text_file'
        assert payload['tools'][0]['type'] == 'function'
        assert not payload['tools'][0]['strict']
        assert not payload['parallel_tool_calls']
        assert not payload['store']
        assert completion.api == 'responses'
        assert completion.response_id == 'resp_123'
        assert completion.tool_calls[0]['call_id'] == 'call_1'
        assert completion.tool_calls[0]['name'] == 'write_text_file'
        assert completion.usage['total_tokens'] == 14
        assert completion.reasoning[0]['summary'][0]['text'] == 'choose write_text_file'

    def test_responses_text_request_uses_json_mode_when_requested(self) -> None:
        response = SimpleNamespace(id='resp_json', model='gpt-test', output_text='{"ok":true}', output=[])
        fake = FakeAsyncOpenAI(responses=FakeResponses(response))
        client = LLMClient(model='gpt-test', api_key='key', api_mode='responses', verbosity='low')
        client._async_client = fake
        content = asyncio.run(client.acomplete([{'role': 'user', 'content': 'return json'}], json_mode=True))
        payload = fake.responses.payloads[0]
        assert content == '{"ok":true}'
        assert payload['text']['format'] == {'type': 'json_object'}
        assert payload['text']['verbosity'] == 'low'

    def test_auto_mode_uses_chat_for_custom_base_url(self) -> None:
        chat_completion = SimpleNamespace(id='chatcmpl_123', model='compat-model', usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2, total_tokens=9), choices=[SimpleNamespace(finish_reason='tool_calls', message=SimpleNamespace(content='', reasoning_content='select process_exit', tool_calls=[SimpleNamespace(id='tool_1', function=SimpleNamespace(name='process_exit', arguments='{"payload":{"ok":true}}'))]))])
        fake = FakeAsyncOpenAI(chat=FakeChat(FakeChatCompletions(chat_completion)))
        client = LLMClient(base_url='https://example.com/compatible/v1', model='compat-model', api_key='key', api_mode='auto')
        client._async_client = fake
        completion = asyncio.run(client.acomplete_action(messages=[{'role': 'user', 'content': 'exit'}], tools=[{'type': 'function', 'function': {'name': 'process_exit', 'description': 'Exit.', 'parameters': {'type': 'object', 'properties': {}}}}]))
        assert fake.chat.completions.payloads[0]['model'] == 'compat-model'
        assert not fake.responses.payloads
        assert completion.api == 'chat'
        assert completion.tool_calls[0]['name'] == 'process_exit'
        assert completion.usage['total_tokens'] == 9
        assert completion.reasoning == 'select process_exit'

    def test_custom_chat_empty_response_retries_with_thinking_disabled(self) -> None:
        empty = SimpleNamespace(id='chatcmpl_empty', model='compat-model', choices=[SimpleNamespace(finish_reason='length', message=SimpleNamespace(content='', tool_calls=[]))])
        ok = SimpleNamespace(id='chatcmpl_ok', model='compat-model', choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='OK', tool_calls=[]))])
        completions = FakeChatCompletions([empty, ok])
        fake = FakeAsyncOpenAI(chat=FakeChat(completions))
        client = LLMClient(base_url='https://example.com/compatible/v1', model='compat-model', api_key='key', api_mode='chat')
        client._async_client = fake
        content = asyncio.run(client.acomplete([{'role': 'user', 'content': 'say OK'}], json_mode=False))
        assert content == 'OK'
        assert len(completions.payloads) == 2
        assert completions.payloads[1]['extra_body'] == {'enable_thinking': False}

class FakeAsyncOpenAI:

    def __init__(self, responses: Any | None=None, chat: Any | None=None):
        self.responses = responses or FakeResponses(SimpleNamespace(id='unused', model='unused', output_text='', output=[]))
        self.chat = chat or FakeChat(FakeChatCompletions(SimpleNamespace(choices=[])))

class FakeResponses:

    def __init__(self, response: Any):
        self.response = response
        self.payloads: list[dict[str, Any]] = []

    async def create(self, **payload: Any) -> Any:
        self.payloads.append(payload)
        return self.response

class FakeChat:

    def __init__(self, completions: Any):
        self.completions = completions

class FakeChatCompletions:

    def __init__(self, completion: Any):
        self.completions = list(completion) if isinstance(completion, list) else [completion]
        self.payloads: list[dict[str, Any]] = []

    async def create(self, **payload: Any) -> Any:
        self.payloads.append(payload)
        return self.completions.pop(0)

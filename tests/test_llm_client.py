from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from agent_libos.llm.client import LLMClient


class LLMClientTests(unittest.TestCase):
    def test_responses_action_request_converts_chat_tools_and_parses_function_calls(self) -> None:
        response = SimpleNamespace(
            id="resp_123",
            model="gpt-test",
            usage=SimpleNamespace(input_tokens=11, output_tokens=3, total_tokens=14),
            output_text="",
            output=[
                SimpleNamespace(type="reasoning", summary=[{"text": "choose write_text_file"}]),
                SimpleNamespace(
                    type="function_call",
                    id="fc_1",
                    call_id="call_1",
                    name="write_text_file",
                    arguments='{"path":"out.txt","content":"ok"}',
                )
            ],
        )
        fake = FakeAsyncOpenAI(responses=FakeResponses(response))
        client = LLMClient(model="gpt-test", api_key="key", api_mode="responses")
        client._async_client = fake

        completion = asyncio.run(
            client.acomplete_action(
                messages=[
                    {"role": "system", "content": "system rules"},
                    {"role": "user", "content": "write a file"},
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "write_text_file",
                            "description": "Write text.",
                            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                        },
                    }
                ],
            )
        )

        payload = fake.responses.payloads[0]
        self.assertEqual(payload["instructions"], "system rules")
        self.assertEqual(payload["input"], [{"role": "user", "content": "write a file"}])
        self.assertEqual(payload["tools"][0]["name"], "write_text_file")
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertFalse(payload["tools"][0]["strict"])
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertFalse(payload["store"])
        self.assertEqual(completion.api, "responses")
        self.assertEqual(completion.response_id, "resp_123")
        self.assertEqual(completion.tool_calls[0]["call_id"], "call_1")
        self.assertEqual(completion.tool_calls[0]["name"], "write_text_file")
        self.assertEqual(completion.usage["total_tokens"], 14)
        self.assertEqual(completion.reasoning[0]["summary"][0]["text"], "choose write_text_file")

    def test_responses_text_request_uses_json_mode_when_requested(self) -> None:
        response = SimpleNamespace(id="resp_json", model="gpt-test", output_text='{"ok":true}', output=[])
        fake = FakeAsyncOpenAI(responses=FakeResponses(response))
        client = LLMClient(model="gpt-test", api_key="key", api_mode="responses", verbosity="low")
        client._async_client = fake

        content = asyncio.run(client.acomplete([{"role": "user", "content": "return json"}], json_mode=True))

        payload = fake.responses.payloads[0]
        self.assertEqual(content, '{"ok":true}')
        self.assertEqual(payload["text"]["format"], {"type": "json_object"})
        self.assertEqual(payload["text"]["verbosity"], "low")

    def test_auto_mode_uses_chat_for_custom_base_url(self) -> None:
        chat_completion = SimpleNamespace(
            id="chatcmpl_123",
            model="compat-model",
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2, total_tokens=9),
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(
                        content="",
                        reasoning_content="select process_exit",
                        tool_calls=[
                            SimpleNamespace(
                                id="tool_1",
                                function=SimpleNamespace(name="process_exit", arguments='{"payload":{"ok":true}}'),
                            )
                        ],
                    ),
                )
            ],
        )
        fake = FakeAsyncOpenAI(chat=FakeChat(FakeChatCompletions(chat_completion)))
        client = LLMClient(
            base_url="https://example.com/compatible/v1",
            model="compat-model",
            api_key="key",
            api_mode="auto",
        )
        client._async_client = fake

        completion = asyncio.run(
            client.acomplete_action(
                messages=[{"role": "user", "content": "exit"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "process_exit",
                            "description": "Exit.",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            )
        )

        self.assertEqual(fake.chat.completions.payloads[0]["model"], "compat-model")
        self.assertFalse(fake.responses.payloads)
        self.assertEqual(completion.api, "chat")
        self.assertEqual(completion.tool_calls[0]["name"], "process_exit")
        self.assertEqual(completion.usage["total_tokens"], 9)
        self.assertEqual(completion.reasoning, "select process_exit")

    def test_custom_chat_empty_response_retries_with_thinking_disabled(self) -> None:
        empty = SimpleNamespace(
            id="chatcmpl_empty",
            model="compat-model",
            choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content="", tool_calls=[]))],
        )
        ok = SimpleNamespace(
            id="chatcmpl_ok",
            model="compat-model",
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="OK", tool_calls=[]))],
        )
        completions = FakeChatCompletions([empty, ok])
        fake = FakeAsyncOpenAI(chat=FakeChat(completions))
        client = LLMClient(
            base_url="https://example.com/compatible/v1",
            model="compat-model",
            api_key="key",
            api_mode="chat",
        )
        client._async_client = fake

        content = asyncio.run(client.acomplete([{"role": "user", "content": "say OK"}], json_mode=False))

        self.assertEqual(content, "OK")
        self.assertEqual(len(completions.payloads), 2)
        self.assertEqual(completions.payloads[1]["extra_body"], {"enable_thinking": False})


class FakeAsyncOpenAI:
    def __init__(self, responses: Any | None = None, chat: Any | None = None):
        self.responses = responses or FakeResponses(SimpleNamespace(id="unused", model="unused", output_text="", output=[]))
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


if __name__ == "__main__":
    unittest.main()

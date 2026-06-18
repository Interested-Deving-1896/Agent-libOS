from __future__ import annotations
import pytest
import asyncio
import tempfile
from agent_libos import Runtime
from agent_libos.llm.client import LLMCompletion
from scripts.human_llm_chat import CHAT_PROCESS_GOAL, EchoResponder, ModelResponder, run_chat

class TestHumanLLMChatScript:

    def test_chat_process_goal_is_plain_prompt_text(self) -> None:
        assert isinstance(CHAT_PROCESS_GOAL, str)
        assert 'Every turn MUST conclude with a tool call' in CHAT_PROCESS_GOAL
        assert 'ask_human' in CHAT_PROCESS_GOAL

    def test_chat_uses_human_question_and_output_tools(self) -> None:
        report = asyncio.run(run_chat(responder=EchoResponder(), max_turns=5, auto_messages=['hello', '/exit'], echo=False))
        assert report['process_status'] == 'exited'
        assert report['turns'] == 1
        assert report['history'] == [{'role': 'user', 'content': 'hello'}, {'role': 'assistant', 'content': 'Echo: hello'}]
        assert 'Assistant: Echo: hello' in report['outputs']
        assert 'Assistant: goodbye.' in report['outputs']
        assert report['actions'] == [None, 'ask_human', 'human_output', None, 'ask_human', 'human_output', 'process_exit']

    def test_model_responder_persists_nested_text_llm_call(self) -> None:
        responder = ModelResponder.__new__(ModelResponder)
        responder.system_prompt = 'System prompt'
        responder.client = FakeTextLLMClient()
        responder._runtime = None
        responder._pid = None
        with tempfile.TemporaryDirectory() as temp_dir:
            db = f'{temp_dir}/runtime.sqlite'
            report = asyncio.run(run_chat(db=db, responder=responder, max_turns=5, auto_messages=['hello', '/exit'], echo=False))
            runtime = Runtime.open(db)
            try:
                calls = [call for call in runtime.store.list_llm_calls(report['pid']) if call.purpose == 'script_human_chat_reply']
                assert len(calls) == 1
                assert calls[0].response_content == 'model reply'
                assert calls[0].usage['total_tokens'] == 6
                assert calls[0].reasoning == {'summary': 'fake text response'}
                assert calls[0].messages[-1]['content'] == 'hello'
            finally:
                runtime.close()

class FakeTextLLMClient:
    model = 'fake-text-model'

    def complete_with_metadata(self, messages, *, json_mode: bool) -> LLMCompletion:
        return LLMCompletion(content='model reply', tool_calls=[], raw={'id': 'fake_raw'}, api='chat', response_id='fake_resp', request_id='fake_req', model=self.model, usage={'prompt_tokens': 4, 'completion_tokens': 2, 'total_tokens': 6}, reasoning={'summary': 'fake text response'})

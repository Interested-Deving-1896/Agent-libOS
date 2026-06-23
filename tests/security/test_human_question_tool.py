from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import pytest
import asyncio
import json
import tempfile
import threading
import time
from agent_libos import Runtime
from agent_libos.models.exceptions import HumanResponseRequired
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import CapabilityRight, HumanRequestStatus, ProcessStatus

class TestHumanQuestionTool:

    def setup_method(self) -> None:
        self.runtime = Runtime.open('local')
        self.human_output: list[str] = []
        self.runtime.substrate.human.output_sink = self.human_output.append

    def teardown_method(self) -> None:
        self.runtime.close()

    def test_ask_human_tool_waits_and_returns_answer_after_queue_processing(self) -> None:
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='ask a human')
        self.runtime.capability.grant(pid, 'human:owner', [CapabilityRight.WRITE], issued_by='test')
        prompts: list[str] = []
        with pytest.raises(HumanResponseRequired) as raised:
            self.runtime.tools.call(pid, 'ask_human', {'question': 'Which color should I use?', 'context': {'artifact': 'draft'}})
        pending = self.runtime.human.pending()[0]
        self.runtime.substrate.human.input_reader = lambda prompt: prompts.append(prompt) or 'blue'
        processed = self.runtime.human.drain_terminal_queue()
        result = self.runtime.tools.call(pid, 'ask_human', {'question': 'Which color should I use?', 'context': {'artifact': 'draft'}})
        assert raised.value.request_id == pending.request_id
        assert pending.payload['type'] == 'question'
        assert self.runtime.process.get(pid).status == ProcessStatus.RUNNABLE
        assert processed[0].status == HumanRequestStatus.APPROVED
        assert processed[0].decision['answer'] == 'blue'
        assert 'artifact' in prompts[0]
        assert result.ok, result.error
        assert result.payload['answer'] == 'blue'
        assert result.payload['request_id'] == pending.request_id

    def test_one_time_ask_human_capability_is_consumed_after_question_is_queued(self) -> None:
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='ask once')
        self.runtime.capability.grant_once(pid, 'human:owner', [CapabilityRight.WRITE], issued_by='test')
        with pytest.raises(HumanResponseRequired):
            self.runtime.tools.call(pid, 'ask_human', {'question': 'Proceed?'})
        pending = self.runtime.human.pending()[0]
        assert not self.runtime.capability.check(pid, 'human:owner', CapabilityRight.WRITE)
        self.runtime.substrate.human.input_reader = lambda _prompt: 'yes'
        self.runtime.human.drain_terminal_queue()
        result = self.runtime.tools.call(pid, 'ask_human', {'question': 'Proceed?'})
        assert result.ok, result.error
        assert result.payload['request_id'] == pending.request_id
        assert result.payload['answer'] == 'yes'

    def test_ask_human_tool_cannot_bypass_human_capability(self) -> None:
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='ask without authority')
        denied = self.runtime.tools.call(pid, 'ask_human', {'question': 'May I ask?'})
        assert not denied.ok
        assert 'lacks write on human:owner' in (denied.error or '')
        assert self.runtime.human.pending() == []
        assert 'human.query' not in self._audit_actions()

    def test_async_runtime_resumes_human_question_with_answer(self) -> None:
        self.runtime.llm.client = PlannedActionClient([{'action': 'ask_human', 'question': 'What deployment window should I use?'}, {'action': 'process_exit', 'payload': {'done': True}}])
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='ask then exit')
        results = asyncio.run(self.runtime.arun_until_idle(max_quanta=4, human_auto_answer='Sunday 02:00 UTC'))
        assert self.runtime.process.get(pid).status == ProcessStatus.EXITED
        assert self.runtime.llm.client.calls == 2
        assert results[0]['waiting_human']
        assert 'action' not in results[0]
        ask_result = next((result for result in results if _action_name(result) == 'ask_human'))
        assert ask_result['result']['payload']['answer'] == 'Sunday 02:00 UTC'
        assert self.runtime.human.list(pid)[0].decision['answer'] == 'Sunday 02:00 UTC'

    def test_pending_ask_human_llm_action_survives_runtime_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = f'{temp_dir}/runtime.sqlite'
            runtime = Runtime.open(db_path)
            try:
                runtime.llm.client = PlannedActionClient([{'action': 'ask_human', 'question': 'Continue after reopen?'}])
                pid = runtime.process.spawn(image='base-agent:v0', goal='ask then reopen')
                waiting = runtime.run_next_process_once()
                request_id = waiting['request_id']
                assert waiting['waiting_human']
            finally:
                runtime.close()

            runtime = Runtime.open(db_path)
            try:
                runtime.llm.client = ExplodingClient()
                runtime.human.drain_terminal_queue(auto_answer='yes')

                resumed = runtime.run_next_process_once()

                assert resumed['resumed_after_human']
                assert resumed['action']['action'] == 'ask_human'
                assert resumed['result']['ok']
                assert resumed['result']['payload']['request_id'] == request_id
                assert resumed['result']['payload']['answer'] == 'yes'
                assert runtime.human.pending() == []
                assert [request.request_id for request in runtime.human.list(pid)] == [request_id]
            finally:
                runtime.close()

    def test_concurrent_identical_ask_human_calls_share_pending_request(self) -> None:
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='ask concurrently')
        self.runtime.capability.grant(pid, 'human:owner', [CapabilityRight.WRITE], issued_by='test')
        original_ask = self.runtime.human.ask

        def slow_ask(*args: object, **kwargs: object) -> str:
            time.sleep(0.05)
            return original_ask(*args, **kwargs)

        self.runtime.human.ask = slow_ask  # type: ignore[method-assign]
        barrier = threading.Barrier(2)

        def call() -> str:
            barrier.wait(timeout=2)
            with pytest.raises(HumanResponseRequired) as raised:
                self.runtime.tools.call(pid, 'ask_human', {'question': 'Same question?'})
            return raised.value.request_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            request_ids = list(executor.map(lambda _: call(), range(2)))

        assert request_ids[0] == request_ids[1]
        assert [request.request_id for request in self.runtime.human.pending()] == [request_ids[0]]

    def _audit_actions(self) -> list[str]:
        return [record.action for record in self.runtime.audit.trace()]

class PlannedActionClient:

    def __init__(self, actions: list[dict[str, object]]):
        self.actions = list(actions)
        self.calls = 0

    def complete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        self.calls += 1
        if not self.actions:
            raise AssertionError('no planned action remains')
        action = self.actions.pop(0)
        name = str(action['action'])
        args = {key: value for key, value in action.items() if key != 'action'}
        return LLMCompletion(content='', tool_calls=[{'id': f'human_question_{self.calls}', 'name': name, 'arguments': json.dumps(args)}])


class ExplodingClient:
    def complete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        raise AssertionError('model should not be called while resuming a pending human action')

def _action_name(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    action = result.get('action')
    if isinstance(action, dict):
        return action.get('action')
    return None

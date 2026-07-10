from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import pytest
import asyncio
import json
import tempfile
import threading
import time
from agent_libos import Runtime
from agent_libos.models.exceptions import HumanResponseRequired, ValidationError
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import (
    CapabilityRight,
    ExternalEffectRollbackStatus,
    HumanRequestStatus,
    ProcessStatus,
)
from agent_libos.substrate import ProviderEffectNotStarted

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
        effects = [
            effect
            for effect in self.runtime.store.list_external_effects(pid=pid)
            if effect.provider == 'human' and effect.operation == 'read'
        ]
        assert len(effects) == 1
        assert effects[0].effect_state == 'finalized'
        assert effects[0].information_flow
        persisted_metadata = json.dumps(effects[0].provider_metadata, sort_keys=True)
        assert 'Which color should I use?' not in persisted_metadata
        assert 'blue' not in persisted_metadata

    def test_auto_answer_write_is_recorded_without_persisting_prompt_or_answer(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='auto answer ledger')
        request_id = self.runtime.human.query(
            pid,
            'owner',
            {'type': 'question', 'question': 'Sensitive deployment secret?'},
            blocking=True,
        )

        processed = self.runtime.human.drain_terminal_queue(auto_answer='private-answer')

        assert processed[0].request_id == request_id
        assert processed[0].status == HumanRequestStatus.APPROVED
        assert processed[0].decision['answer'] == 'private-answer'
        effects = [
            effect
            for effect in self.runtime.store.list_external_effects(pid=pid)
            if effect.provider == 'human' and effect.operation == 'write'
        ]
        assert len(effects) == 1
        assert effects[0].effect_state == 'finalized'
        metadata = json.dumps(effects[0].provider_metadata, sort_keys=True)
        assert 'Sensitive deployment secret?' not in metadata
        assert 'private-answer' not in metadata

    def test_auto_answer_post_provider_classifier_failure_keeps_pending_without_reprompt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='auto answer sink failure')
        request_id = self.runtime.human.query(
            pid,
            'owner',
            {'type': 'question', 'question': 'Prompt exactly once?'},
            blocking=True,
        )

        def fail_classifier(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError('classifier unavailable')

        monkeypatch.setattr(self.runtime.human.provider, 'classify_external_effect', fail_classifier)
        processed = self.runtime.human.drain_terminal_queue(auto_answer='yes')

        assert processed[0].request_id == request_id
        assert processed[0].decision['answer'] == 'yes'
        assert self.runtime.human.drain_terminal_queue(auto_answer='yes') == []
        assert len(self.human_output) == 1
        effects = [effect for effect in self.runtime.store.list_external_effects(pid=pid) if effect.provider == 'human']
        assert len(effects) == 1
        assert effects[0].effect_state == 'pending'

    @pytest.mark.parametrize('certified_not_started', [False, True])
    def test_terminal_read_failure_preserves_pending_request_and_effect_semantics(
        self,
        certified_not_started: bool,
    ) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='terminal read failure')
        request_id = self.runtime.human.query(
            pid,
            'owner',
            {'type': 'question', 'question': 'Do not persist this prompt'},
            blocking=True,
        )

        def fail_read(_prompt: str) -> str:
            if certified_not_started:
                raise ProviderEffectNotStarted('read did not start')
            raise RuntimeError('ambiguous terminal read failure')

        self.runtime.substrate.human.input_reader = fail_read
        expected = ProviderEffectNotStarted if certified_not_started else RuntimeError
        with pytest.raises(expected):
            self.runtime.human.process_next_terminal()

        assert self.runtime.human.get(request_id).status == HumanRequestStatus.PENDING
        effects = [effect for effect in self.runtime.store.list_external_effects(pid=pid) if effect.provider == 'human']
        if certified_not_started:
            assert effects == []
        else:
            assert len(effects) == 1
            assert effects[0].effect_state == 'finalized'
            assert effects[0].rollback_status == ExternalEffectRollbackStatus.UNKNOWN
            metadata = json.dumps(effects[0].provider_metadata, sort_keys=True)
            assert 'Do not persist this prompt' not in metadata
            assert 'ambiguous terminal read failure' not in metadata

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

    def test_question_cannot_be_approved_without_a_typed_answer(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='question needs answer')
        request_id = self.runtime.human.query(
            pid,
            'owner',
            {'type': 'question', 'question': 'Which environment?'},
            blocking=True,
        )

        with pytest.raises(ValidationError, match='answer'):
            self.runtime.human.approve(request_id, {'approved': True})
        with pytest.raises(ValidationError, match='answer'):
            self.runtime.human.approve(request_id, {'approved': True, 'answer': '   '})

        assert self.runtime.human.get(request_id).status == HumanRequestStatus.PENDING
        assert self.runtime.process.get(pid).status == ProcessStatus.WAITING_HUMAN

    def test_multiple_blocking_requests_keep_process_waiting_until_all_are_decided(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='wait for all questions')
        first = self.runtime.human.query(
            pid,
            'owner',
            {'type': 'question', 'question': 'First?'},
            blocking=True,
        )
        second = self.runtime.human.query(
            pid,
            'owner',
            {'type': 'question', 'question': 'Second?'},
            blocking=True,
        )

        self.runtime.human.approve(first, {'approved': True, 'answer': 'one'})
        assert self.runtime.process.get(pid).status == ProcessStatus.WAITING_HUMAN

        self.runtime.human.approve(second, {'approved': True, 'answer': 'two'})
        assert self.runtime.process.get(pid).status == ProcessStatus.RUNNABLE

    @pytest.mark.parametrize('terminal_action', ['cancel', 'exit'])
    def test_terminal_process_cancels_pending_human_requests(self, terminal_action: str) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='terminal request cleanup')
        request_id = self.runtime.human.query(
            pid,
            'owner',
            {'type': 'question', 'question': 'Too late?'},
            blocking=True,
        )

        if terminal_action == 'cancel':
            self.runtime.process.cancel(pid, 'test cancellation')
        else:
            self.runtime.process.exit(pid, message='test exit')

        request = self.runtime.human.get(request_id)
        assert request.status == HumanRequestStatus.CANCELLED
        assert request.decision is not None
        assert request.decision['reason']
        with pytest.raises(ValidationError, match='not pending'):
            self.runtime.human.approve(request_id, {'approved': True, 'answer': 'late'})

    def test_terminal_exit_does_not_wait_for_blocked_human_provider_read(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='cancel blocked human read')
        request_id = self.runtime.human.query(
            pid,
            'owner',
            {'type': 'question', 'question': 'May block forever?'},
            blocking=True,
        )
        read_started = threading.Event()
        release_read = threading.Event()
        terminal_errors: list[BaseException] = []
        exit_errors: list[BaseException] = []

        def blocked_read(_prompt: str) -> str:
            read_started.set()
            assert release_read.wait(timeout=2)
            return 'late answer'

        def drain() -> None:
            try:
                self.runtime.human.process_next_terminal()
            except BaseException as exc:
                terminal_errors.append(exc)

        def exit_process() -> None:
            try:
                self.runtime.process.exit(pid, message='cancel pending question')
            except BaseException as exc:
                exit_errors.append(exc)

        self.runtime.substrate.human.input_reader = blocked_read
        terminal = threading.Thread(target=drain)
        terminal.start()
        assert read_started.wait(timeout=1)
        exiting = threading.Thread(target=exit_process)
        exiting.start()
        exiting.join(timeout=0.5)
        try:
            assert not exiting.is_alive()
            assert exit_errors == []
            assert self.runtime.process.get(pid).status == ProcessStatus.EXITED
            assert self.runtime.human.get(request_id).status == HumanRequestStatus.CANCELLED
        finally:
            release_read.set()
            terminal.join(timeout=2)
            exiting.join(timeout=2)
        assert len(terminal_errors) == 1
        assert isinstance(terminal_errors[0], ValidationError)

    def test_terminal_exit_does_not_wait_for_blocked_human_provider_write(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='exit during human output')
        self.runtime.capability.grant(pid, 'human:owner', [CapabilityRight.WRITE], issued_by='test')
        write_started = threading.Event()
        release_write = threading.Event()
        output_errors: list[BaseException] = []
        exit_errors: list[BaseException] = []

        def blocked_write(_message: str) -> None:
            write_started.set()
            assert release_write.wait(timeout=2)

        def deliver() -> None:
            try:
                self.runtime.human.output(pid, 'committed before exit')
            except BaseException as exc:
                output_errors.append(exc)

        def exit_process() -> None:
            try:
                self.runtime.process.exit(pid, message='exit while output provider blocks')
            except BaseException as exc:
                exit_errors.append(exc)

        self.runtime.substrate.human.output_sink = blocked_write
        output = threading.Thread(target=deliver)
        output.start()
        assert write_started.wait(timeout=1)
        exiting = threading.Thread(target=exit_process)
        exiting.start()
        exiting.join(timeout=0.5)
        try:
            assert not exiting.is_alive()
            assert exit_errors == []
            assert self.runtime.process.get(pid).status == ProcessStatus.EXITED
        finally:
            release_write.set()
            output.join(timeout=2)
            exiting.join(timeout=2)
        assert output_errors == []
        assert self.runtime.human.list(pid)[0].status == HumanRequestStatus.DELIVERED

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

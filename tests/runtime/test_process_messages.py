from __future__ import annotations
import pytest
import json
import asyncio
import tempfile
from typing import Any
from agent_libos import Runtime
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import ProcessMessageKind, ProcessStatus
from agent_libos.runtime.syscalls import LibOSSyscallSession

class TestProcessMessage:

    def test_process_message_tools_send_read_and_ack_related_processes(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='base-agent:v0', goal='parent')
            child = runtime.spawn_child_process(parent, 'child')
            sent = runtime.tools.call(parent, 'send_process_message', {'recipient_pid': child, 'kind': 'normal', 'subject': 'status', 'body': 'send a status update', 'payload': {'priority': 1}})
            assert sent.ok, sent.error
            assert len(runtime.messages.unread(child)) == 1
            read = runtime.tools.call(child, 'read_process_messages', {})
            assert read.ok, read.error
            assert read.payload['messages'][0]['subject'] == 'status'
            assert read.payload['messages'][0]['payload'] == {'priority': 1}
            assert read.payload['messages'][0]['status'] == 'acked'
            assert read.payload['acked_message_ids'] == [sent.payload['message_id']]
            assert runtime.messages.unread(child) == []
        finally:
            runtime.close()

    def test_unrelated_process_cannot_send_process_message(self) -> None:
        runtime = Runtime.open('local')
        try:
            first = runtime.process.spawn(image='base-agent:v0', goal='first')
            second = runtime.process.spawn(image='base-agent:v0', goal='second')
            denied = runtime.tools.call(first, 'send_process_message', {'recipient_pid': second, 'body': 'no'})
            assert not denied.ok
            assert 'can only message' in (denied.error or '')
            assert runtime.messages.unread(second) == []
        finally:
            runtime.close()

    def test_human_can_send_normal_and_interrupt_process_messages(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='listen to human')
            normal = runtime.human.send_process_message(pid, 'please check progress', subject='status')
            interrupt = runtime.human.send_process_message(pid, 'stop current work and inspect this', kind=ProcessMessageKind.INTERRUPT)
            unread = runtime.messages.unread(pid)
            assert [message.message_id for message in unread] == [normal.message_id, interrupt.message_id]
            assert unread[0].sender == 'human:owner'
            assert unread[0].channel == 'human'
            assert unread[0].payload['source'] == 'human_input'
            assert unread[1].kind == ProcessMessageKind.INTERRUPT
            assert 'human.message' in _audit_actions(runtime)
        finally:
            runtime.close()

    def test_process_message_syscalls_send_read_and_ack(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='base-agent:v0', goal='parent')
            child = runtime.spawn_child_process(parent, 'child')
            parent_session = LibOSSyscallSession(runtime, parent)
            child_session = LibOSSyscallSession(runtime, child)
            sent = asyncio.run(parent_session.handle('process.send_message', {'recipient_pid': child, 'kind': 'normal', 'subject': 'via syscall', 'body': 'hello'}))
            read = asyncio.run(child_session.handle('process.read_messages', {}))
            assert sent['subject'] == 'via syscall'
            assert read['messages'][0]['message_id'] == sent['message_id']
            assert read['messages'][0]['status'] == 'acked'
            assert runtime.messages.unread(child) == []
        finally:
            runtime.close()

    def test_process_message_filters_channel_correlation_reply_and_ids(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='base-agent:v0', goal='parent')
            child = runtime.spawn_child_process(parent, 'child')
            first = runtime.messages.send_from_process(parent, child, channel='control', correlation_id='job-1', subject='request', body='start')
            runtime.messages.send_from_process(parent, child, channel='noise', correlation_id='job-1', subject='ignore')
            reply = runtime.messages.send_from_process(child, parent, channel='control', correlation_id='job-1', reply_to=first.message_id, subject='reply')
            selected = runtime.tools.call(child, 'read_process_messages', {'channel': 'control', 'correlation_id': 'job-1', 'ack': False})
            reply_selected = runtime.tools.call(parent, 'read_process_messages', {'reply_to': first.message_id, 'message_ids': [reply.message_id]})
            assert selected.ok, selected.error
            assert [message['message_id'] for message in selected.payload['messages']] == [first.message_id]
            assert selected.payload['messages'][0]['channel'] == 'control'
            assert selected.payload['messages'][0]['correlation_id'] == 'job-1'
            assert selected.payload['acked_message_ids'] == []
            assert len(runtime.messages.unread(child)) == 2
            assert reply_selected.ok, reply_selected.error
            assert reply_selected.payload['messages'][0]['reply_to'] == first.message_id
            assert reply_selected.payload['acked_message_ids'] == [reply.message_id]
            assert runtime.messages.unread(parent) == []
        finally:
            runtime.close()

    def test_receive_process_messages_blocks_until_matching_message_then_resumes(self) -> None:
        client = PlannedActionClient([{'action': 'receive_process_messages', 'channel': 'control', 'correlation_id': 'job-1'}])
        runtime = Runtime.open('local')
        runtime.llm.client = client
        try:
            parent = runtime.process.spawn(image='base-agent:v0', goal='parent')
            child = runtime.spawn_child_process(parent, 'wait for control message')
            waiting = runtime.run_process_once(child)
            assert waiting['waiting_message']
            assert waiting['filters']['channel'] == 'control'
            assert runtime.process.get(child).status == ProcessStatus.WAITING_EVENT
            assert len(client.user_prompts) == 1
            runtime.messages.send_from_process(parent, child, channel='noise', correlation_id='job-1', subject='not yet')
            assert runtime.process.get(child).status == ProcessStatus.WAITING_EVENT
            skipped = runtime.run_process_once(child)
            assert skipped['skipped']
            assert len(client.user_prompts) == 1
            matching = runtime.messages.send_from_process(parent, child, channel='control', correlation_id='job-1', subject='resume', payload={'ready': True})
            assert runtime.process.get(child).status == ProcessStatus.RUNNABLE
            resumed = runtime.run_process_once(child)
            assert resumed['ok']
            assert resumed['resumed_after_message']
            assert resumed['action']['action'] == 'receive_process_messages'
            assert resumed['result']['payload']['messages'][0]['message_id'] == matching.message_id
            assert resumed['result']['payload']['messages'][0]['payload'] == {'ready': True}
            assert resumed['result']['payload']['acked_message_ids'] == [matching.message_id]
            assert len(client.user_prompts) == 1
            assert [message.subject for message in runtime.messages.unread(child)] == ['not yet']
            assert 'process.message.wait_wake' in _audit_actions(runtime)
        finally:
            runtime.close()

    def test_receive_message_syscall_waits_inside_single_syscall_until_matching_message(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.scheduler.poll_interval_s = 0.001
            parent = runtime.process.spawn(image='base-agent:v0', goal='parent')
            child = runtime.spawn_child_process(parent, 'wait via syscall')
            child_session = LibOSSyscallSession(runtime, child)

            async def scenario() -> dict[str, Any]:
                task = asyncio.create_task(child_session.handle('process.receive_messages', {'block': True, 'channel': 'control', 'correlation_id': 'job-1'}))
                await asyncio.sleep(0.01)
                assert not task.done()
                assert runtime.process.get(child).status == ProcessStatus.WAITING_EVENT
                runtime.messages.send_from_process(parent, child, channel='noise', correlation_id='job-1', subject='not matching')
                await asyncio.sleep(0.01)
                assert not task.done()
                assert runtime.process.get(child).status == ProcessStatus.WAITING_EVENT
                matching = runtime.messages.send_from_process(parent, child, channel='control', correlation_id='job-1', subject='matching')
                result = await asyncio.wait_for(task, timeout=1.0)
                result['expected_message_id'] = matching.message_id
                return result
            result = asyncio.run(scenario())
            assert result['ready']
            assert result['messages'][0]['message_id'] == result['expected_message_id']
            assert result['messages'][0]['status'] == 'acked'
            assert result['acked_message_ids'] == [result['expected_message_id']]
            assert runtime.process.get(child).status == ProcessStatus.RUNNABLE
        finally:
            runtime.close()

    def test_receive_message_syscall_blocks_by_default(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.scheduler.poll_interval_s = 0.001
            parent = runtime.process.spawn(image='base-agent:v0', goal='parent')
            child = runtime.spawn_child_process(parent, 'default receive')
            child_session = LibOSSyscallSession(runtime, child)

            async def scenario() -> dict[str, Any]:
                task = asyncio.create_task(child_session.handle('process.receive_messages', {'channel': 'control'}))
                await asyncio.sleep(0.01)
                assert not task.done()
                assert runtime.process.get(child).status == ProcessStatus.WAITING_EVENT
                matching = runtime.messages.send_from_process(parent, child, channel='control')
                result = await asyncio.wait_for(task, timeout=1.0)
                result['expected_message_id'] = matching.message_id
                return result
            result = asyncio.run(scenario())
            assert result['ready']
            assert result['messages'][0]['message_id'] == result['expected_message_id']
            assert runtime.process.get(child).status == ProcessStatus.RUNNABLE
        finally:
            runtime.close()

    def test_process_messages_are_durable_in_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = f'{temp_dir}/runtime.sqlite'
            runtime = Runtime.open(db)
            pid = runtime.process.spawn(image='base-agent:v0', goal='persist queue')
            message = runtime.messages.post(sender='test', recipient_pid=pid, subject='persisted')
            runtime.close()
            reopened = Runtime.open(db)
            try:
                unread = reopened.messages.unread(pid)
                assert [item.message_id for item in unread] == [message.message_id]
                assert unread[0].subject == 'persisted'
            finally:
                reopened.close()

    def test_interrupt_message_preempts_tool_call_until_read(self) -> None:
        client = PlannedActionClient([{'action': 'get_current_time', 'timezone': 'UTC'}, {'action': 'read_process_messages'}])
        runtime = Runtime.open('local')
        runtime.llm.client = client
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='handle interrupts')
            runtime.messages.post(sender='test', recipient_pid=pid, kind=ProcessMessageKind.INTERRUPT, subject='urgent', body='inspect this before other work')
            interrupted = runtime.run_process_once(pid)
            assert interrupted['result']['interrupted_by_message']
            assert interrupted['result']['message_notice']['phase'] == 'before_tool_call'
            assert 'primitive.clock.now' not in _audit_actions(runtime)
            assert 'process_message_notice' in client.user_prompts[0]
            read = runtime.run_process_once(pid)
            assert read['action']['action'] == 'read_process_messages'
            assert read['result']['payload']['messages'][0]['kind'] == 'interrupt'
            assert runtime.messages.unread(pid, kind=ProcessMessageKind.INTERRUPT) == []
        finally:
            runtime.close()

    def test_normal_message_notifies_after_tool_call_without_preempting(self) -> None:
        client = PlannedActionClient([{'action': 'get_current_time', 'timezone': 'UTC'}])
        runtime = Runtime.open('local')
        runtime.llm.client = client
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='handle normal messages')
            runtime.messages.post(sender='test', recipient_pid=pid, kind=ProcessMessageKind.NORMAL, subject='later', body='read after current tool')
            result = runtime.run_process_once(pid)
            assert result['action']['action'] == 'get_current_time'
            assert 'primitive.clock.now' in _audit_actions(runtime)
            assert result['result']['message_notice']['phase'] == 'after_tool_call'
            assert result['result']['message_notice']['kind'] == 'normal'
            assert len(runtime.messages.unread(pid, kind=ProcessMessageKind.NORMAL)) == 1
        finally:
            runtime.close()

class PlannedActionClient:

    def __init__(self, actions: list[dict[str, Any]]):
        self.actions = list(actions)
        self.user_prompts: list[str] = []

    def complete_action(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMCompletion:
        if not self.actions:
            raise AssertionError('no planned action remains')
        self.user_prompts.append(str(messages[-1]['content']))
        action = self.actions.pop(0)
        name = str(action['action'])
        args = {key: value for key, value in action.items() if key != 'action'}
        return LLMCompletion(content='', tool_calls=[{'id': f'message_{len(self.user_prompts)}', 'name': name, 'arguments': json.dumps(args)}])

def _audit_actions(runtime: Runtime) -> set[str]:
    return {record.action for record in runtime.audit.trace()}

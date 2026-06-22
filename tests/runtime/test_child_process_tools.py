from __future__ import annotations
import pytest
import asyncio
import json
from typing import Any
from agent_libos import Runtime
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import CapabilityRight, ObjectType, ProcessStatus, ResourceBudget
from agent_libos.models.exceptions import NotFound, ProcessError, ProcessWaitRequired
from scripts.llm_context_probe import last_tool_result, static_prefix

class TestChildProcessTool:

    def test_fork_wait_tool_blocks_parent_until_child_exits_and_exposes_result(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.scheduler.poll_interval_s = 1.0
            client = ParentChildClient()
            runtime.llm.client = client
            parent = runtime.process.spawn(image='base-agent:v0', goal='fork child and wait')
            results = asyncio.run(runtime.arun_until_idle(max_quanta=8))
            assert runtime.process.get(parent).status == ProcessStatus.EXITED
            assert client.child_pid is not None
            assert client.child_pid is not None
            assert runtime.process.get(client.child_pid).status == ProcessStatus.EXITED
            assert any((isinstance(result, dict) and result.get('waiting_event') for result in results))
            wait_result = next((result for result in results if _action_name(result) == 'wait_child_process'))
            result_oid = wait_result['result']['payload']['result_oid']
            child_result = runtime.store.get_object(result_oid)
            assert child_result is not None
            assert child_result is not None
            assert child_result.payload['value'] == 42
            parent_view = runtime.process.get(parent).memory_view
            assert parent_view is not None
            assert parent_view is not None
            assert result_oid in [handle.oid for handle in parent_view.roots]
            assert 'process.wait_wake' in [record.action for record in runtime.audit.trace()]
        finally:
            runtime.close()

    def test_child_list_signal_and_budget_are_enforced(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='base-agent:v0', goal='manage one child', resource_budget=ResourceBudget(max_child_processes=1))
            other = runtime.process.spawn(image='base-agent:v0', goal='not a child')
            forked = runtime.tools.call(parent, 'fork_child_process', {'goal': 'child', 'include_parent_roots': False})
            assert forked.ok, forked.error
            child = forked.payload['child_pid']
            listed = runtime.tools.call(parent, 'list_child_processes', {})
            assert listed.ok, listed.error
            assert [entry['pid'] for entry in listed.payload['children']] == [child]
            assert listed.payload['children'][0]['working_directory'] == '.'
            paused = runtime.tools.call(parent, 'signal_child_process', {'child_pid': child, 'signal': 'pause'})
            assert paused.ok, paused.error
            assert paused.payload['status'] == 'paused'
            resumed = runtime.tools.call(parent, 'signal_child_process', {'child_pid': child, 'signal': 'resume'})
            assert resumed.ok, resumed.error
            assert resumed.payload['status'] == 'runnable'
            denied_signal = runtime.tools.call(parent, 'signal_child_process', {'child_pid': other, 'signal': 'pause'})
            assert not denied_signal.ok
            assert 'not a child' in (denied_signal.error or '')
            denied_fork = runtime.tools.call(parent, 'fork_child_process', {'goal': 'second child'})
            assert not denied_fork.ok
            assert 'exhausted child process budget' in (denied_fork.error or '')
        finally:
            runtime.close()

    def test_nonblocking_wait_child_process_does_not_suspend_parent(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='base-agent:v0', goal='poll child')
            child = runtime.spawn_child_process(parent, 'still running')

            waited = runtime.tools.call(parent, 'wait_child_process', {'child_pid': child, 'block': False})

            assert waited.ok, waited.error
            assert waited.payload['ready'] is False
            assert runtime.process.get(parent).status == ProcessStatus.RUNNABLE
            assert runtime.process.get(parent).status_message is None
        finally:
            runtime.close()

    def test_terminal_process_cannot_be_resumed_by_signal(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='exit once')
            runtime.process.exit(pid, message='done')

            with pytest.raises(ProcessError, match='cannot signal terminal process'):
                runtime.process.resume(pid)

            assert runtime.process.get(pid).status == ProcessStatus.EXITED
        finally:
            runtime.close()

    def test_resource_kill_wakes_parent_waiting_on_child(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='base-agent:v0', goal='wait for killed child')
            child = runtime.spawn_child_process(parent, 'will be killed')
            with pytest.raises(ProcessWaitRequired):
                runtime.process.wait(parent, child)
            assert runtime.process.get(parent).status == ProcessStatus.WAITING_EVENT

            runtime.resources.kill_if_exceeded(child, reason='test budget exhausted')

            assert runtime.process.get(child).status == ProcessStatus.KILLED
            assert runtime.process.get(parent).status == ProcessStatus.RUNNABLE
            assert runtime.process.get(parent).status_message is None
        finally:
            runtime.close()

    def test_spawn_child_process_creates_fresh_child_without_parent_memory_or_default_caps(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='review-agent:v0', goal='parent')
            parent_note = runtime.memory.create_object(pid=parent, object_type='observation', name='parent.note', payload={'visible_to_parent': True})
            spawned = runtime.tools.call(parent, 'spawn_child_process', {'goal': 'fresh child', 'image': 'coding-agent:v0'})
            assert spawned.ok, spawned.error
            child = runtime.process.get(spawned.payload['child_pid'])
            assert child.parent_pid == parent
            assert child.image_id == 'coding-agent:v0'
            assert 'read_text_file' in child.tool_table
            assert parent_note.oid not in [handle.oid for handle in child.memory_view.roots]
            assert [handle.oid for handle in child.memory_view.roots] == [child.goal_oid]
            read_resource = runtime.filesystem.resource_for_path('README.md')
            assert not runtime.capability.check(child.pid, read_resource, CapabilityRight.READ)
        finally:
            runtime.close()

    def test_spawn_child_process_inherits_only_explicit_capabilities(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='review-agent:v0', goal='parent')
            runtime.filesystem.grant_path(parent, 'README.md', [CapabilityRight.READ], issued_by='test')
            spawned = runtime.tools.call(parent, 'spawn_child_process', {'goal': 'read one file', 'inherit_read_files': ['README.md']})
            assert spawned.ok, spawned.error
            child = runtime.process.get(spawned.payload['child_pid'])
            allowed = runtime.filesystem.resource_for_path('README.md')
            other = runtime.filesystem.resource_for_path('pyproject.toml')
            assert runtime.capability.check(child.pid, allowed, CapabilityRight.READ)
            assert not runtime.capability.check(child.pid, other, CapabilityRight.READ)
        finally:
            runtime.close()

    def test_exec_process_swaps_image_without_granting_target_image_capabilities(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='become coding agent')
            runtime.filesystem.grant_workspace(pid, [CapabilityRight.READ], issued_by='test')
            executed = runtime.tools.call(pid, 'exec_process', {'image': 'coding-agent:v0', 'goal': 'inspect without automatic capability lift', 'preserve_capabilities': False, 'preserve_memory': False})
            assert executed.ok, executed.error
            process = runtime.process.get(pid)
            assert process.image_id == 'coding-agent:v0'
            assert 'read_text_file' in process.tool_table
            assert 'spawn_child_process' in process.tool_table
            read_resource = runtime.filesystem.resource_for_path('README.md')
            assert not runtime.capability.check(pid, read_resource, CapabilityRight.READ)
            assert [handle.oid for handle in process.memory_view.roots] == [process.goal_oid]
            assert 'process.exec' in [record.action for record in runtime.audit.trace()]
        finally:
            runtime.close()

    def test_failed_exec_process_rolls_back_to_previous_process_state(self) -> None:
        runtime = Runtime.open('local')
        try:
            runtime.register_image(
                {
                    'image_id': 'failing-exec:v0',
                    'name': 'failing-exec',
                },
                actor='cli',
            )
            pid = runtime.process.spawn(image='base-agent:v0', goal='stay on base')
            other = runtime.process.spawn(image='base-agent:v0', goal='unrelated')
            before = runtime.process.get(pid)
            before_tools = dict(before.tool_table)
            original_configure_skills = runtime._configure_process_skills_for_image

            def fail_after_unrelated_mutation(target_pid: str, image_id: str, assigned_by: str) -> None:
                other_process = runtime.process.get(other)
                other_process.status_message = 'must survive scoped rollback'
                runtime.store.update_process(other_process)
                raise RuntimeError('skill boot failed')

            runtime._configure_process_skills_for_image = fail_after_unrelated_mutation

            with pytest.raises(RuntimeError):
                runtime.exec_process(pid, 'failing-exec:v0', goal='should not apply')

            after = runtime.process.get(pid)
            assert after.status == ProcessStatus.RUNNABLE
            assert after.image_id == 'base-agent:v0'
            assert after.goal_oid == before.goal_oid
            assert after.tool_table == before_tools
            assert runtime.process.get(other).status_message == 'must survive scoped rollback'
            runtime._configure_process_skills_for_image = original_configure_skills
        finally:
            runtime.close()

    def test_merge_child_memory_tool_adds_child_view_objects_to_parent(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='base-agent:v0', goal='merge child')
            child = runtime.process.fork(parent, goal='produce result')
            created = runtime.tools.call(child, 'create_memory_object', {'name': 'child.result', 'type': 'summary', 'payload': {'merged': True}})
            assert created.ok, created.error
            result_oid = created.payload['oid']
            runtime.tools.call(child, 'process_exit', {'result_oid': result_oid})
            merged = runtime.tools.call(parent, 'merge_child_memory', {'child_pid': child})
            assert merged.ok, merged.error
            assert result_oid in merged.payload['merged_oids']
            parent_view = runtime.process.get(parent).memory_view
            assert parent_view is not None
            assert parent_view is not None
            assert result_oid in [handle.oid for handle in parent_view.roots]
        finally:
            runtime.close()

    def test_fork_root_oids_do_not_upgrade_read_only_objects_to_materialize(self) -> None:
        runtime = Runtime.open('local')
        try:
            owner = runtime.process.spawn(image='base-agent:v0', goal='owner')
            parent = runtime.process.spawn(image='base-agent:v0', goal='fork read-only root')
            secret = runtime.memory.create_object(
                pid=owner,
                object_type=ObjectType.EVIDENCE,
                payload={'secret': 'child must not materialize this'},
                name='read.only.secret',
            )
            runtime.capability.grant(parent, f'object:{secret.oid}', [CapabilityRight.READ], issued_by='test')

            forked = runtime.tools.call(
                parent,
                'fork_child_process',
                {'goal': 'child', 'include_parent_roots': False, 'root_oids': [secret.oid]},
            )

            assert forked.ok, forked.error
            child = runtime.process.get(forked.payload['child_pid'])
            root = next(handle for handle in child.memory_view.roots if handle.oid == secret.oid)
            assert root.rights == {'read'}
            context = runtime.memory.materialize_context(child.pid, child.memory_view)
            assert secret.oid in context.omitted_objects
            assert 'child must not materialize this' not in context.text
        finally:
            runtime.close()

    def test_process_exit_result_oid_requires_object_read_authority(self) -> None:
        runtime = Runtime.open('local')
        try:
            owner = runtime.process.spawn(image='base-agent:v0', goal='owner')
            process = runtime.process.spawn(image='base-agent:v0', goal='try unauthorized result oid')
            secret = runtime.memory.create_object(
                pid=owner,
                object_type=ObjectType.EVIDENCE,
                payload={'secret': 'not a result'},
                name='private.result',
            )

            exited = runtime.tools.call(process, 'process_exit', {'result_oid': secret.oid})

            assert not exited.ok
            assert 'lacks read' in (exited.error or '')
            assert runtime.process.get(process).status == ProcessStatus.RUNNABLE
        finally:
            runtime.close()

    def test_fork_does_not_resurrect_revoked_image_default_capability(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='coding-agent:v0', goal='fork after revoke')
            path = 'README.md'
            for cap in list(runtime.capability.capabilities_for(parent)):
                if cap.resource == 'filesystem:workspace:*' and CapabilityRight.READ.value in cap.rights:
                    runtime.capability.revoke(cap.cap_id, revoked_by='cli', reason='revoked before fork')
            forked = runtime.tools.call(parent, 'fork_child_process', {'goal': 'try reading'})
            assert forked.ok, forked.error
            child = forked.payload['child_pid']
            denied = runtime.tools.call(child, 'read_text_file', {'path': path})
            assert not denied.ok
            assert 'lacks read' in (denied.error or '')
        finally:
            runtime.close()

class ParentChildClient:

    def __init__(self) -> None:
        self.parent_pid: str | None = None
        self.child_pid: str | None = None
        self.parent_step = 0
        self.calls = 0

    async def acomplete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        return self.complete_action(messages, tools)

    def complete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        self.calls += 1
        pid = _pid_from_messages(messages)
        parent_pid = _parent_pid_from_messages(messages)
        if parent_pid is not None:
            return self._completion('process_exit', {'payload': {'child_pid': pid, 'value': 42}})
        self.parent_pid = pid
        if self.parent_step == 0:
            self.parent_step = 1
            return self._completion('fork_child_process', {'goal': 'return value 42', 'mode': 'worker', 'include_parent_roots': False})
        if self.parent_step == 1:
            self.child_pid = _last_tool_result(messages, 'fork_child_process')['child_pid']
            self.parent_step = 2
            return self._completion('wait_child_process', {'child_pid': self.child_pid})
        if self.parent_step == 2:
            wait_result = _last_tool_result(messages, 'wait_child_process')
            self.parent_step = 3
            return self._completion('process_exit', {'payload': {'waited': wait_result['ready'], 'child_pid': wait_result['child_pid']}})
        raise AssertionError('parent action plan is complete')

    def _completion(self, name: str, args: dict[str, Any]) -> LLMCompletion:
        return LLMCompletion(content='', tool_calls=[{'id': f'child_process_{self.calls}', 'name': name, 'arguments': json.dumps(args)}])

def _pid_from_messages(messages: list[dict[str, str]]) -> str:
    pid = static_prefix(messages).get('pid')
    if not isinstance(pid, str) or not pid:
        raise AssertionError('prompt did not include process pid')
    return pid

def _parent_pid_from_messages(messages: list[dict[str, str]]) -> str | None:
    value = static_prefix(messages).get('parent_pid')
    if value is None or isinstance(value, str):
        return value
    raise AssertionError('prompt parent pid had an unexpected shape')

def _last_tool_result(messages: list[dict[str, str]], tool_name: str) -> dict[str, Any]:
    result = last_tool_result(messages, tool_name)
    if result is not None:
        return result
    raise AssertionError(f'no visible result for {tool_name}')

def _action_name(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    action = result.get('action')
    if isinstance(action, dict):
        return action.get('action')
    return None

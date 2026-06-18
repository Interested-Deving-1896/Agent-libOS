from __future__ import annotations
import pytest
import contextlib
import io
import json
import tempfile
from pathlib import Path
from agent_libos import Runtime
from agent_libos.api.cli import main as cli_main
from agent_libos.models import CapabilityEffect, CapabilityRight, CapabilitySpec
from agent_libos.models.exceptions import CapabilityDenied, ValidationError
from agent_libos.runtime.syscalls import LibOSSyscallSession

class TestCapabilityV2Manager:

    def test_typed_resource_matching_rejects_prefix_collision(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='resource matching')
            runtime.capability.grant(pid, 'filesystem:workspace:src/*', [CapabilityRight.READ], issued_by='test')
            assert runtime.capability.check(pid, 'filesystem:workspace:src/main.py', CapabilityRight.READ)
            assert runtime.capability.check(pid, 'filesystem:workspace:src', CapabilityRight.READ)
            assert not runtime.capability.check(pid, 'filesystem:workspace:src2/main.py', CapabilityRight.READ)
            with pytest.raises(CapabilityDenied):
                runtime.capability.parse_resource_pattern('filesystem:workspace:src*')
            with pytest.raises(CapabilityDenied):
                runtime.capability.grant(pid, '*', [CapabilityRight.READ], issued_by='test')
            with pytest.raises(ValidationError):
                runtime.capability.grant(pid, 'filesystem:workspace:src/main.py', ['*'], issued_by='test')
        finally:
            runtime.close()

    def test_deny_dominates_matching_allow(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='deny dominates')
            runtime.capability.grant(pid, 'filesystem:workspace:*', [CapabilityRight.READ], issued_by='test')
            runtime.capability.issue_trusted(pid, 'filesystem:workspace:secret.txt', [CapabilityRight.READ], issued_by='test', effect=CapabilityEffect.DENY)
            decision = runtime.capability.authorize(pid, 'filesystem:workspace:secret.txt', CapabilityRight.READ)
            assert not decision.allowed
            assert decision.effect == CapabilityEffect.DENY
            assert decision.matched_capability_ids
        finally:
            runtime.close()

    def test_one_shot_capability_is_consumed_after_successful_use(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='one shot')
            runtime.capability.grant_once(pid, 'filesystem:workspace:once.txt', [CapabilityRight.WRITE], issued_by='test')
            assert runtime.capability.permission_policy(pid, 'filesystem:workspace:once.txt', CapabilityRight.WRITE) == runtime.capability.ALLOW_ONCE
            runtime.capability.consume_allow_once(pid, 'filesystem:workspace:once.txt', CapabilityRight.WRITE, used_by=pid)
            assert not runtime.capability.check(pid, 'filesystem:workspace:once.txt', CapabilityRight.WRITE)
        finally:
            runtime.close()

    def test_permission_policy_constraint_is_converted_not_evaluated_as_runtime_policy(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='policy conversion')
            converted = runtime.capability.grant(pid, 'object:converted-policy', [CapabilityRight.READ], issued_by='test', constraints={runtime.capability.POLICY_KEY: runtime.capability.ALWAYS_DENY})
            injected = runtime.capability.issue_trusted(pid, 'object:injected-policy', [CapabilityRight.READ], issued_by='test', constraints={runtime.capability.POLICY_KEY: runtime.capability.ALWAYS_ALLOW})
            converted_decision = runtime.capability.authorize(pid, 'object:converted-policy', CapabilityRight.READ)
            injected_decision = runtime.capability.authorize(pid, 'object:injected-policy', CapabilityRight.READ)
            assert converted.effect == CapabilityEffect.DENY
            assert not converted_decision.allowed
            assert not injected_decision.allowed
            assert not injected_decision.constraint_results[runtime.capability.POLICY_KEY]['ok']
        finally:
            runtime.close()

    def test_issue_requires_trusted_actor_or_grant_authority(self) -> None:
        runtime = Runtime.open('local')
        try:
            issuer = runtime.process.spawn(image='base-agent:v0', goal='issuer')
            subject = runtime.process.spawn(image='base-agent:v0', goal='subject')
            spec = CapabilitySpec(resource='object:alpha', rights={CapabilityRight.READ.value})
            with pytest.raises(CapabilityDenied):
                runtime.capability.issue(issuer, subject, spec)
            runtime.capability.issue_trusted(issuer, 'object:alpha', [CapabilityRight.GRANT], issued_by='test')
            cap = runtime.capability.issue(issuer, subject, spec)
            assert runtime.capability.check(subject, 'object:alpha', CapabilityRight.READ)
            assert cap.issuer_cap_id == runtime.capability.capabilities_for(issuer)[-1].cap_id
        finally:
            runtime.close()

    def test_actor_names_cannot_gain_trust_by_prefix(self) -> None:
        runtime = Runtime.open('local')
        try:
            subject = runtime.process.spawn(image='base-agent:v0', goal='subject')
            for actor in ['memoryevil', 'image:evil', 'jit.evil', 'process.fork:evil']:
                with pytest.raises(CapabilityDenied):
                    runtime.capability.issue(actor, subject, CapabilitySpec(resource='object:prefix-collision', rights={CapabilityRight.READ.value}))
        finally:
            runtime.close()

    def test_delegate_can_only_attenuate_delegable_parent_capability(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='base-agent:v0', goal='parent')
            child = runtime.spawn_child_process(parent, 'child')
            runtime.capability.grant(parent, 'filesystem:workspace:src/*', [CapabilityRight.READ, CapabilityRight.WRITE], issued_by='test', delegable=True)
            delegated = runtime.capability.delegate(parent, child, CapabilitySpec(resource='filesystem:workspace:src/main.py', rights={CapabilityRight.READ.value}))
            assert delegated.parent_cap_id == runtime.capability.capabilities_for(parent)[-1].cap_id
            assert runtime.capability.check(child, 'filesystem:workspace:src/main.py', CapabilityRight.READ)
            assert not runtime.capability.check(child, 'filesystem:workspace:src/main.py', CapabilityRight.WRITE)
            with pytest.raises(CapabilityDenied):
                runtime.capability.delegate(parent, child, CapabilitySpec(resource='filesystem:workspace:other.py', rights={CapabilityRight.READ.value}))
        finally:
            runtime.close()

    def test_delegate_cannot_drop_parent_constraints(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='base-agent:v0', goal='parent')
            child = runtime.spawn_child_process(parent, 'child')
            policy_cap = runtime.capability.grant(parent, 'shell:*', [CapabilityRight.EXECUTE], issued_by='test', constraints={runtime.config.shell.policy_capability_key: runtime.config.shell.always_allow_level}, delegable=True)
            with pytest.raises(CapabilityDenied):
                runtime.capability.delegate(parent, child, CapabilitySpec(resource='shell:git', rights={CapabilityRight.EXECUTE.value}))
            delegated = runtime.capability.delegate(parent, child, CapabilitySpec(resource='shell:*', rights={CapabilityRight.EXECUTE.value}, constraints=dict(policy_cap.constraints)))
            assert delegated.constraints == policy_cap.constraints
        finally:
            runtime.close()

    def test_revoke_requires_holder_issuer_or_revoke_authority(self) -> None:
        runtime = Runtime.open('local')
        try:
            owner = runtime.process.spawn(image='base-agent:v0', goal='owner')
            stranger = runtime.process.spawn(image='base-agent:v0', goal='stranger')
            cap = runtime.capability.grant(owner, 'object:revocable', [CapabilityRight.READ], issued_by='test')
            with pytest.raises(CapabilityDenied):
                runtime.capability.revoke(cap.cap_id, revoked_by=stranger)
            runtime.capability.revoke(cap.cap_id, revoked_by=owner, reason='holder abandoned')
            assert not runtime.capability.check(owner, 'object:revocable', CapabilityRight.READ)
        finally:
            runtime.close()

    def test_holder_cannot_self_revoke_restrictive_capability(self) -> None:
        runtime = Runtime.open('local')
        try:
            owner = runtime.process.spawn(image='base-agent:v0', goal='owner')
            deny = runtime.capability.issue_trusted(owner, 'filesystem:workspace:secret.txt', [CapabilityRight.READ], issued_by='test', effect=CapabilityEffect.DENY)
            with pytest.raises(CapabilityDenied):
                runtime.capability.revoke(deny.cap_id, revoked_by=owner)
            assert not runtime.capability.check(owner, 'filesystem:workspace:secret.txt', CapabilityRight.READ)
            runtime.capability.revoke(deny.cap_id, revoked_by='test')
            assert runtime.capability.inspect(deny.cap_id)['status'] == 'revoked'
        finally:
            runtime.close()

class TestCapabilityV2RuntimeInterface:

    def test_default_images_expose_only_low_risk_capability_tools(self) -> None:
        runtime = Runtime.open('local')
        try:
            pid = runtime.process.spawn(image='base-agent:v0', goal='tool table')
            process = runtime.process.get(pid)
            assert 'list_capabilities' in process.tool_table
            assert 'inspect_capability' in process.tool_table
            assert 'delegate_capability' not in process.tool_table
            assert 'revoke_capability' not in process.tool_table
            listed = runtime.tools.call(pid, 'list_capabilities', {})
            assert listed.ok, listed.error
            assert listed.payload['capabilities']
        finally:
            runtime.close()

    def test_capability_syscalls_do_not_bypass_authority(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='base-agent:v0', goal='parent')
            child = runtime.spawn_child_process(parent, 'child')
            other = runtime.process.spawn(image='base-agent:v0', goal='other')
            runtime.capability.grant(parent, 'object:shared', [CapabilityRight.READ], issued_by='test', delegable=True)
            parent_session = LibOSSyscallSession(runtime, parent)
            other_session = LibOSSyscallSession(runtime, other)
            listed = self._run(parent_session.handle('capability.list', {}))
            delegated = self._run(parent_session.handle('capability.delegate', {'child_pid': child, 'resource': 'object:shared', 'rights': [CapabilityRight.READ.value]}))
            assert listed['capabilities']
            assert runtime.capability.check(child, 'object:shared', CapabilityRight.READ)
            assert delegated['capability']['subject'] == child
            with pytest.raises(CapabilityDenied):
                self._run(other_session.handle('capability.inspect', {'capability_id': delegated['capability']['cap_id']}))
            with pytest.raises(CapabilityDenied):
                self._run(parent_session.handle('capability.delegate', {'child_pid': other, 'resource': 'object:shared', 'rights': [CapabilityRight.READ.value]}))
            deny = runtime.capability.issue_trusted(parent, 'object:blocked', [CapabilityRight.READ], issued_by='test', effect=CapabilityEffect.DENY)
            with pytest.raises(CapabilityDenied):
                self._run(parent_session.handle('capability.revoke', {'capability_id': deny.cap_id}))
        finally:
            runtime.close()

    def test_spawn_child_invalid_capability_inheritance_is_preflighted(self) -> None:
        runtime = Runtime.open('local')
        try:
            parent = runtime.process.spawn(image='base-agent:v0', goal='parent')
            runtime.capability.grant(parent, 'shell:*', [CapabilityRight.EXECUTE], issued_by='test', constraints={runtime.config.shell.policy_capability_key: runtime.config.shell.always_allow_level}, delegable=True)
            before = len(runtime.process.list())
            with pytest.raises(CapabilityDenied):
                runtime.spawn_child_process(parent, 'should fail', inherit_capabilities=[{'resource': 'shell:git', 'rights': [CapabilityRight.EXECUTE.value]}])
            assert len(runtime.process.list()) == before
        finally:
            runtime.close()

    def test_capabilities_cli_outputs_stable_json_and_enforces_actor_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / 'runtime.sqlite')
            parent = _run_cli_json(['--db', db_path, 'spawn', '--goal', 'parent'])
            granted = _run_cli_json(['--db', db_path, 'capabilities', 'grant', parent['pid'], 'object:cli', '--rights', 'read', '--delegable'])
            listed = _run_cli_json(['--db', db_path, 'capabilities', 'list', '--subject', parent['pid']])
            explained = _run_cli_json(['--db', db_path, 'capabilities', 'explain', parent['pid'], 'object:cli', 'read'])
            assert granted['subject'] == parent['pid']
            assert granted['cap_id'] in {capability['cap_id'] for capability in listed}
            assert explained['allowed']
            with pytest.raises(CapabilityDenied):
                _run_cli_json(['--db', db_path, 'capabilities', '--actor-pid', parent['pid'], 'grant', 'other', 'object:denied', '--rights', 'read'])

    def _run(self, awaitable):
        import asyncio
        return asyncio.run(awaitable)

def _run_cli_json(argv: list[str]):
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        cli_main(argv)
    return json.loads(stdout.getvalue())

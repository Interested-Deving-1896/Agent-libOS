from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_libos import Runtime
from agent_libos.config import AgentLibOSConfig, SkillDefaults
from agent_libos.models import AgentImage, CapabilityRight
from agent_libos.models.exceptions import CapabilityDenied, HumanApprovalRequired, NotFound, ValidationError
from agent_libos.runtime.syscalls import LibOSSyscallSession
from agent_libos.substrate import LocalResourceProviderSubstrate
from tests.support.skills import write_raw_skill, write_skill_package


class TestSkillPackageLoading:
    def test_skill_discovery_window_reports_registered_packages_beyond_requested_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Runtime.open('local')
            try:
                for index in range(3):
                    skill_dir = write_skill_package(root, f'window-skill-{index}', allowed_tools=['echo'])
                    package, _source = runtime.skills._load_package_from_host_path(skill_dir)
                    runtime.skills.register_skill_package(package, actor='cli', require_capability=False)

                bounded, has_more = runtime.skills.discover_skills_window(
                    actor='test',
                    require_capability=False,
                    limit=2,
                )
                complete, complete_has_more = runtime.skills.discover_skills_window(
                    actor='test',
                    require_capability=False,
                    limit=3,
                )

                assert len(bounded) == 2
                assert has_more is True
                assert len(complete) == 3
                assert complete_has_more is False
            finally:
                runtime.close()

    def test_skill_discovery_rejects_unbounded_limits(self) -> None:
        runtime = Runtime.open('local')
        try:
            for limit in (0, -1, True, runtime.config.skills.discover_limit + 1):
                with pytest.raises(ValidationError, match='limit'):
                    runtime.skills.discover_skills(require_capability=False, limit=limit)  # type: ignore[arg-type]
        finally:
            runtime.close()


    def test_standard_package_validation_and_global_trust(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            global_dir = root / 'global-skills'
            skill_dir = write_skill_package(global_dir, 'global-skill', allowed_tools=['echo'])
            config = AgentLibOSConfig(skills=replace(SkillDefaults(), global_dirs=(str(global_dir),)))
            runtime = Runtime.open('local', config=config)
            try:
                with pytest.raises(CapabilityDenied):
                    runtime.skills.register_global_skill_from_path(skill_dir, actor='cli', require_capability=False)
                trust = runtime.skills.global_package_info(skill_dir)
                runtime.skills.trust_skill_source(actor='cli', source_type='global', source=trust['source'], package_sha256=trust['package_sha256'], require_capability=False)
                registered = runtime.skills.register_global_skill_from_path(skill_dir, actor='cli', require_capability=False)
                assert registered['skill_id'] == 'global-skill'
                assert registered['source_type'] == 'global'
                assert 'package_sha256' in registered
                with pytest.raises(ValidationError):
                    runtime.skills.validate_package_path(write_raw_skill(root, 'bad', 'name: bad\ndescription: Bad\nunknown: nope\n'))
                with pytest.raises(ValidationError):
                    runtime.skills.validate_package_path(write_raw_skill(root, 'BadName', 'name: BadName\ndescription: Bad\n'))
                with pytest.raises(ValidationError):
                    runtime.skills.validate_package_path(write_raw_skill(root, 'bad-metadata', 'name: bad-metadata\ndescription: Bad\nmetadata: {agent-libos.version: 1}\n'))
                old_yaml = root / 'legacy.yaml'
                old_yaml.write_text('schema_version: 1\nskill_id: legacy:v0\nname: Legacy\n', encoding='utf-8')
                with pytest.raises(ValidationError):
                    runtime.skills.validate_package_path(old_yaml)
                with pytest.raises(ValidationError):
                    runtime.skills.register_skill_package({'schema_version': 1, 'skill_id': 'legacy', 'name': 'legacy', 'description': 'Legacy shape.', 'tools': ['echo']}, actor='cli', require_capability=False)
                with pytest.raises(ValidationError):
                    write_skill_package(root, 'bad-jit', jit_tools=[{'name': 'bad', 'description': 'bad', 'source_path': '../escaped.ts'}])
                with pytest.raises(ValidationError):
                    write_skill_package(root, 'bad-right', required_capabilities=[{'resource': 'filesystem:workspace:*', 'rights': ['*']}])
            finally:
                runtime.close()

    def test_failed_skill_replace_rolls_back_registry_and_restores_one_time_write(self, monkeypatch) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = write_skill_package(root, 'atomic-skill', allowed_tools=['echo'], body='original instructions\n')
            runtime = Runtime.open('local')
            try:
                original, _source = runtime.skills._load_package_from_host_path(skill_dir)
                runtime.skills.register_skill_package(original, actor='cli', require_capability=False)
                write_skill_package(root, 'atomic-skill', allowed_tools=['human_output'], body='replacement instructions\n')
                replacement, _source = runtime.skills._load_package_from_host_path(skill_dir)
                actor = runtime.process.spawn(image='base-agent:v0', goal='replace skill')
                cap = runtime.capability.grant_once(
                    actor,
                    'skill:atomic-skill',
                    [CapabilityRight.WRITE],
                    issued_by='test',
                )
                real_record = runtime.audit.record

                def fail_registration_audit(*args, **kwargs):
                    if kwargs.get('action') == 'skill.register':
                        raise RuntimeError('registration audit failed')
                    return real_record(*args, **kwargs)

                monkeypatch.setattr(runtime.audit, 'record', fail_registration_audit)
                with pytest.raises(RuntimeError, match='registration audit failed'):
                    runtime.skills.register_skill_package(replacement, actor=actor, replace=True)

                persisted, _metadata = runtime.store.get_skill('atomic-skill')
                assert persisted.package_sha256 == original.package_sha256
                assert persisted.allowed_tools == ['echo']
                assert runtime.store.get_capability(cap.cap_id).uses_remaining == 1
                assert not any(
                    event.type.value == 'skill_registered' and event.source == actor
                    for event in runtime.events.list()
                )
            finally:
                runtime.close()

    def test_failed_skill_trust_rolls_back_record_and_restores_one_time_admin(self, monkeypatch) -> None:
        runtime = Runtime.open('local')
        try:
            actor = runtime.process.spawn(image='base-agent:v0', goal='trust skill')
            cap = runtime.capability.grant_once(
                actor,
                runtime.config.skills.trust_resource,
                [CapabilityRight.ADMIN],
                issued_by='test',
            )
            real_emit = runtime.events.emit

            def fail_trust_event(event_type, *args, **kwargs):
                if str(getattr(event_type, 'value', event_type)) == 'skill_trusted':
                    raise RuntimeError('trust event failed')
                return real_emit(event_type, *args, **kwargs)

            monkeypatch.setattr(runtime.events, 'emit', fail_trust_event)
            with pytest.raises(RuntimeError, match='trust event failed'):
                runtime.skills.trust_skill_source(
                    actor=actor,
                    source_type='global',
                    source='global/example',
                    package_sha256='a' * 64,
                )

            assert not runtime.store.is_skill_trusted(
                source_type='global',
                source='global/example',
                package_sha256='a' * 64,
            )
            assert runtime.store.get_capability(cap.cap_id).uses_remaining == 1
        finally:
            runtime.close()

    @pytest.mark.parametrize('operation', ['trust', 'untrust'])
    def test_skill_trust_reauthorizes_unlimited_admin_before_mutation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        operation: str,
    ) -> None:
        runtime = Runtime.open('local')
        source_type = 'global'
        source = 'global/reauthorization'
        package_sha256 = 'b' * 64
        try:
            actor = runtime.process.spawn(
                image='base-agent:v0',
                goal=f'skill trust authority race {operation}',
            )
            if operation == 'untrust':
                runtime.skills.trust_skill_source(
                    actor='test.host',
                    source_type=source_type,
                    source=source,
                    package_sha256=package_sha256,
                    require_capability=False,
                )
            authority = runtime.capability.grant(
                actor,
                runtime.config.skills.trust_resource,
                [CapabilityRight.ADMIN],
                issued_by='test.host',
            )
            original_require = runtime.capability.require

            def revoke_after_outer_authorization(*args: Any, **kwargs: Any):
                decision = original_require(*args, **kwargs)
                runtime.capability.revoke(
                    authority.cap_id,
                    revoked_by='test.host',
                    reason='skill trust revocation race regression',
                    require_authority=False,
                )
                return decision

            monkeypatch.setattr(runtime.capability, 'require', revoke_after_outer_authorization)

            with pytest.raises(CapabilityDenied, match='authority changed'):
                if operation == 'trust':
                    runtime.skills.trust_skill_source(
                        actor=actor,
                        source_type=source_type,
                        source=source,
                        package_sha256=package_sha256,
                    )
                else:
                    runtime.skills.untrust_skill_source(
                        actor=actor,
                        source_type=source_type,
                        source=source,
                        package_sha256=package_sha256,
                    )

            persisted = runtime.store.is_skill_trusted(
                source_type=source_type,
                source=source,
                package_sha256=package_sha256,
            )
            assert persisted is (operation == 'untrust')
        finally:
            runtime.close()

    def test_skill_registration_reauthorizes_inside_publication_transaction(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(
                Path(temp_dir),
                'registration-race-skill',
                allowed_tools=['echo'],
            )
            runtime = Runtime.open('local')
            try:
                package, _source = runtime.skills._load_package_from_host_path(skill_dir)
                actor = runtime.process.spawn(goal='skill registration authority race')
                authority = runtime.capability.grant(
                    actor,
                    'skill:registration-race-skill',
                    [CapabilityRight.WRITE],
                    issued_by='test.host',
                )
                original_transaction = runtime.capability.authority_transaction

                def revoke_before_publication(decisions, *, actor: str, operation: str):
                    if operation == 'skill registration':
                        runtime.capability.revoke(
                            authority.cap_id,
                            revoked_by='test.host',
                            reason='registration race regression',
                            require_authority=False,
                        )
                    return original_transaction(
                        decisions,
                        actor=actor,
                        operation=operation,
                    )

                monkeypatch.setattr(
                    runtime.capability,
                    'authority_transaction',
                    revoke_before_publication,
                )

                with pytest.raises(CapabilityDenied, match='authority changed'):
                    runtime.skills.register_skill_package(package, actor=actor)

                assert runtime.store.get_skill('registration-race-skill') is None
            finally:
                runtime.close()

    def test_global_skill_registration_rechecks_exact_trust_inside_transaction(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            global_dir = root / 'global-skills'
            skill_dir = write_skill_package(
                global_dir,
                'global-registration-race',
                allowed_tools=['echo'],
            )
            config = AgentLibOSConfig(
                skills=replace(SkillDefaults(), global_dirs=(str(global_dir),))
            )
            runtime = Runtime.open('local', config=config)
            try:
                trust = runtime.skills.global_package_info(skill_dir)
                runtime.skills.trust_skill_source(
                    actor='cli',
                    source_type='global',
                    source=trust['source'],
                    package_sha256=trust['package_sha256'],
                    require_capability=False,
                )
                original_transaction = runtime.capability.authority_transaction

                def untrust_before_publication(decisions, *, actor: str, operation: str):
                    if operation == 'skill registration':
                        runtime.skills.store.delete_skill_trust(
                            source_type='global',
                            source=trust['source'],
                            package_sha256=trust['package_sha256'],
                        )
                    return original_transaction(
                        decisions,
                        actor=actor,
                        operation=operation,
                    )

                monkeypatch.setattr(
                    runtime.capability,
                    'authority_transaction',
                    untrust_before_publication,
                )

                with pytest.raises(CapabilityDenied, match='not trusted'):
                    runtime.skills.register_global_skill_from_path(
                        skill_dir,
                        actor='cli',
                        require_capability=False,
                    )

                assert runtime.store.get_skill('global-registration-race') is None
            finally:
                runtime.close()

    def test_skill_activation_reauthorizes_inside_publication_transaction(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(
                Path(temp_dir),
                'activation-race-skill',
                allowed_tools=['echo'],
            )
            runtime = Runtime.open('local')
            try:
                runtime.skills.register_skill_from_path(
                    skill_dir,
                    actor='cli',
                    require_capability=False,
                )
                actor = runtime.process.spawn(goal='skill activation authority race')
                authority = runtime.capability.grant(
                    actor,
                    'skill:activation-race-skill',
                    [CapabilityRight.EXECUTE],
                    issued_by='test.host',
                )
                original_transaction = runtime.capability.authority_transaction

                def revoke_before_publication(decisions, *, actor: str, operation: str):
                    if operation == 'skill activation':
                        runtime.capability.revoke(
                            authority.cap_id,
                            revoked_by='test.host',
                            reason='activation race regression',
                            require_authority=False,
                        )
                    return original_transaction(
                        decisions,
                        actor=actor,
                        operation=operation,
                    )

                monkeypatch.setattr(
                    runtime.capability,
                    'authority_transaction',
                    revoke_before_publication,
                )

                with pytest.raises(CapabilityDenied, match='authority changed'):
                    runtime.skills.activate_skill(
                        actor,
                        'activation-race-skill',
                        actor=actor,
                    )

                assert 'activation-race-skill' not in runtime.process.get(actor).loaded_skills
                assert 'echo' not in runtime.process.get(actor).tool_table
            finally:
                runtime.close()

    def test_skill_activation_rejects_failed_reservation_settlement_atomically(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(
                Path(temp_dir),
                'activation-settlement-skill',
                allowed_tools=['echo'],
            )
            runtime = Runtime.open('local')
            try:
                runtime.skills.register_skill_from_path(
                    skill_dir,
                    actor='cli',
                    require_capability=False,
                )
                actor = runtime.process.spawn(goal='skill activation settlement')
                authority = runtime.capability.grant_once(
                    actor,
                    'skill:activation-settlement-skill',
                    [CapabilityRight.EXECUTE],
                    issued_by='test.host',
                )
                monkeypatch.setattr(
                    runtime.capability,
                    'commit_reserved_use',
                    lambda *args, **kwargs: False,
                )

                with pytest.raises(CapabilityDenied, match='reservation is no longer active'):
                    runtime.skills.activate_skill(
                        actor,
                        'activation-settlement-skill',
                        actor=actor,
                    )

                persisted = runtime.store.get_capability(authority.cap_id)
                assert persisted is not None
                assert persisted.active
                assert persisted.uses_remaining == 1
                assert 'activation-settlement-skill' not in runtime.process.get(actor).loaded_skills
                assert 'echo' not in runtime.process.get(actor).tool_table
            finally:
                runtime.close()

    def test_workspace_register_and_activate_reads_via_filesystem_and_uses_human_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            write_skill_package(Path(temp_dir), 'workspace-skill', allowed_tools=['echo'], extra_resources={'references/guide.md': 'Workspace resource guide.'})
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='load workspace skill')
                runtime.filesystem.grant_path(pid, 'workspace-skill/SKILL.md', [CapabilityRight.READ], issued_by='test')
                runtime.filesystem.grant_directory(pid, 'workspace-skill/references', [CapabilityRight.READ], issued_by='test')
                with pytest.raises(HumanApprovalRequired) as raised:
                    runtime.skills.activate_skill_from_workspace_path(pid, 'workspace-skill')
                runtime.human.approve(raised.value.request_id)
                loaded = runtime.skills.activate_skill_from_workspace_path(pid, 'workspace-skill')
                assert loaded['skill_id'] == 'workspace-skill'
                assert 'echo' in runtime.process.get(pid).tool_table
                assert not runtime.capability.check(pid, 'skill:workspace-skill', CapabilityRight.EXECUTE)
                resource = runtime.skills.read_skill_resource(pid, 'workspace-skill', 'references/guide.md')
                assert resource['content'] == 'Workspace resource guide.'
            finally:
                runtime.close()

    def test_workspace_activate_failure_keeps_committed_write_one_shot_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            write_skill_package(Path(temp_dir), 'broken-skill', allowed_tools=['missing_workspace_tool'])
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='load broken workspace skill')
                runtime.filesystem.grant_path(pid, 'broken-skill/SKILL.md', [CapabilityRight.READ], issued_by='test')
                write_cap = runtime.capability.grant_once(
                    pid,
                    'skill:broken-skill',
                    [CapabilityRight.WRITE],
                    issued_by='test',
                )
                execute_cap = runtime.capability.grant_once(
                    pid,
                    'skill:broken-skill',
                    [CapabilityRight.EXECUTE],
                    issued_by='test',
                )

                with pytest.raises(NotFound, match='tool not found'):
                    runtime.skills.activate_skill_from_workspace_path(pid, 'broken-skill')

                assert runtime.store.get_skill('broken-skill') is not None
                assert runtime.store.get_capability(write_cap.cap_id).uses_remaining == 0
                assert runtime.store.get_capability(execute_cap.cap_id).uses_remaining == 1
                assert 'broken-skill' not in runtime.process.get(pid).loaded_skills
            finally:
                runtime.close()

    def test_host_skill_package_rejects_hardlinked_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside:
            root = Path(temp_dir)
            skill_dir = write_skill_package(root, 'hardlink-skill', allowed_tools=['echo'], extra_resources={'references/guide.md': 'resource'})
            outside_file = Path(outside) / 'external-secret.txt'
            outside_file.write_text('external secret\n', encoding='utf-8')
            resource = skill_dir / 'references' / 'guide.md'
            resource.unlink()
            try:
                os.link(outside_file, resource)
            except OSError:
                pytest.skip('hardlink creation is not available in this environment')
            runtime = Runtime.open('local')
            try:
                with pytest.raises(ValidationError, match='hard links'):
                    runtime.skills.validate_package_path(skill_dir)
            finally:
                runtime.close()

    def test_package_hash_binds_instructions_and_resource_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = write_skill_package(
                root,
                'integrity-skill',
                allowed_tools=['echo'],
                extra_resources={'references/guide.md': 'resource-v1\n'},
                body='# integrity-skill\n\ninstruction-v1\n',
            )
            runtime = Runtime.open('local')
            try:
                first = runtime.skills.validate_package_path(skill_dir)['package_sha256']

                write_skill_package(
                    root,
                    'integrity-skill',
                    allowed_tools=['echo'],
                    extra_resources={'references/guide.md': 'resource-v1\n'},
                    body='# integrity-skill\n\ninstruction-v2\n',
                )
                instruction_changed = runtime.skills.validate_package_path(skill_dir)['package_sha256']

                write_skill_package(
                    root,
                    'integrity-skill',
                    allowed_tools=['echo'],
                    extra_resources={'references/guide.md': 'resource-v2\n'},
                    body='# integrity-skill\n\ninstruction-v1\n',
                )
                resource_changed = runtime.skills.validate_package_path(skill_dir)['package_sha256']

                assert first != instruction_changed
                assert first != resource_changed
                assert instruction_changed != resource_changed
            finally:
                runtime.close()

    def test_loaded_skill_snapshot_hash_rejects_tampered_resource_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(
                Path(temp_dir),
                'tamper-skill',
                allowed_tools=['echo'],
                extra_resources={'references/guide.md': 'resource-v1\n'},
            )
            runtime = Runtime.open('local')
            try:
                package, _source = runtime.skills._load_package_from_host_path(skill_dir)
                snapshot = runtime.skills._skill_snapshot(package)
                for resource in snapshot['resources']:
                    if resource['path'] == 'references/guide.md':
                        resource['content'] = 'resource-v2\n'
                        break

                with pytest.raises(ValidationError, match='snapshot hash'):
                    runtime.skills._package_from_snapshot(snapshot, context='tampered skill')
            finally:
                runtime.close()

    def test_skill_syscalls_use_primitive_capabilities_not_tool_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            write_skill_package(Path(temp_dir), 'syscall-skill', allowed_tools=['echo'])
            runtime = Runtime.open('local', substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='syscall skill')
                process = runtime.process.get(pid)
                process.tool_table.pop('activate_skill', None)
                runtime.store.update_process(process)
                runtime.filesystem.grant_path(pid, 'syscall-skill/SKILL.md', [CapabilityRight.READ], issued_by='test')
                runtime.capability.grant(pid, 'skill:syscall-skill', [CapabilityRight.WRITE, CapabilityRight.EXECUTE], issued_by='test')
                registered = self._run(LibOSSyscallSession(runtime, pid).handle('skill.register_path', {'path': 'syscall-skill'}))
                loaded = self._run(LibOSSyscallSession(runtime, pid).handle('skill.activate', {'skill_id': 'syscall-skill'}))
                assert registered['skill_id'] == 'syscall-skill'
                assert loaded['skill_id'] == 'syscall-skill'
                assert 'echo' in runtime.process.get(pid).tool_table
                with pytest.raises(NotFound):
                    self._run(LibOSSyscallSession(runtime, pid).handle('skill.register', {'skill': {'schema_version': 1, 'skill_id': 'inline-skill', 'name': 'inline-skill', 'description': 'Inline package should not be syscall-visible.', 'instructions': 'inline'}}))
            finally:
                runtime.close()

    def test_loaded_existing_tool_visibility_does_not_grant_resource_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(Path(temp_dir), 'read-skill', allowed_tools=['read_text_file'])
            runtime = Runtime.open('local')
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='load read tool')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                runtime.capability.grant(pid, 'skill:read-skill', [CapabilityRight.EXECUTE], issued_by='test')
                runtime.skills.activate_skill(pid, 'read-skill', actor=pid)
                result = runtime.tools.call(pid, 'read_text_file', {'path': 'secret.txt'})
                assert 'read_text_file' in runtime.process.get(pid).tool_table
                assert not runtime.capability.check(pid, 'filesystem:workspace:secret.txt', CapabilityRight.READ)
                assert not result.ok
                assert 'lacks read' in (result.error or '')
            finally:
                runtime.close()

    def test_read_skill_resource_requires_loaded_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(Path(temp_dir), 'resource-skill', allowed_tools=['echo'], extra_resources={'references/guide.md': 'Remember resource-token.\n'})
            runtime = Runtime.open('local')
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='read resource')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                with pytest.raises(CapabilityDenied):
                    runtime.skills.read_skill_resource(pid, 'resource-skill', 'references/guide.md')
                runtime.capability.grant(pid, 'skill:resource-skill', [CapabilityRight.EXECUTE], issued_by='test')
                runtime.skills.activate_skill(pid, 'resource-skill', actor=pid)
                resource = runtime.skills.read_skill_resource(pid, 'resource-skill', 'references/guide.md')
                assert 'resource-token' in resource['content']
            finally:
                runtime.close()

    def test_loaded_skill_uses_activation_snapshot_after_registry_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = write_skill_package(
                root,
                'snapshot-skill',
                allowed_tools=['echo'],
                extra_resources={'references/guide.md': 'original-resource-token\n'},
                body='# snapshot-skill\n\nUse original-instruction-token.\n',
            )
            runtime = Runtime.open('local')
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='snapshot skill')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                runtime.capability.grant(pid, 'skill:snapshot-skill', [CapabilityRight.EXECUTE], issued_by='test')
                runtime.skills.activate_skill(pid, 'snapshot-skill', actor=pid)

                write_skill_package(
                    root,
                    'snapshot-skill',
                    allowed_tools=['human_output'],
                    extra_resources={'references/guide.md': 'replaced-resource-token\n'},
                    body='# snapshot-skill\n\nUse replaced-instruction-token.\n',
                )
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', replace=True, require_capability=False)

                context = runtime.skills.prompt_context(pid)[0]
                resource = runtime.skills.read_skill_resource(pid, 'snapshot-skill', 'references/guide.md')

                assert 'original-instruction-token' in context['instructions']
                assert 'replaced-instruction-token' not in context['instructions']
                assert context['allowed_tools'] == ['echo']
                assert resource['content'].replace('\r\n', '\n') == 'original-resource-token\n'
            finally:
                runtime.close()

    def test_checkpoint_restore_and_fork_do_not_resurrect_global_skill_trust(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            global_dir = root / 'global'
            skill_dir = write_skill_package(global_dir, 'trust-checkpoint-skill', allowed_tools=['echo'])
            config = AgentLibOSConfig(skills=replace(SkillDefaults(), global_dirs=(str(global_dir),)))
            runtime = Runtime.open('local', config=config)
            try:
                trust = runtime.skills.global_package_info(skill_dir)
                runtime.skills.trust_skill_source(
                    actor='cli',
                    source_type='global',
                    source=trust['source'],
                    package_sha256=trust['package_sha256'],
                    require_capability=False,
                )
                runtime.skills.register_global_skill_from_path(skill_dir, actor='cli', require_capability=False)
                pid = runtime.process.spawn(image='base-agent:v0', goal='checkpoint trust')
                runtime.capability.grant(pid, 'skill:trust-checkpoint-skill', [CapabilityRight.EXECUTE], issued_by='test')
                runtime.skills.activate_skill(pid, 'trust-checkpoint-skill', actor=pid)
                checkpoint_id = runtime.checkpoint.create(pid, 'trusted skill loaded', actor=pid)

                runtime.skills.untrust_skill_source(
                    actor='cli',
                    source_type='global',
                    source=trust['source'],
                    package_sha256=trust['package_sha256'],
                    require_capability=False,
                )
                assert not runtime.store.is_skill_trusted(
                    source_type='global',
                    source=trust['source'],
                    package_sha256=trust['package_sha256'],
                )

                runtime.checkpoint.restore('cli', checkpoint_id, require_capability=False)
                assert not runtime.store.is_skill_trusted(
                    source_type='global',
                    source=trust['source'],
                    package_sha256=trust['package_sha256'],
                )
                runtime.checkpoint.fork_from_checkpoint('cli', checkpoint_id, require_capability=False)
                assert not runtime.store.is_skill_trusted(
                    source_type='global',
                    source=trust['source'],
                    package_sha256=trust['package_sha256'],
                )
            finally:
                runtime.close()

    def test_cross_process_skill_activate_requires_target_process_admin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(Path(temp_dir), 'cross-load-skill', allowed_tools=['echo'])
            runtime = Runtime.open('local')
            try:
                actor = runtime.process.spawn(image='base-agent:v0', goal='actor')
                target = runtime.process.spawn(image='base-agent:v0', goal='target')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                runtime.capability.grant(actor, 'skill:cross-load-skill', [CapabilityRight.EXECUTE], issued_by='test')
                with pytest.raises(CapabilityDenied):
                    runtime.skills.activate_skill(target, 'cross-load-skill', actor=actor)
                runtime.capability.grant(actor, f'process:{target}', [CapabilityRight.ADMIN], issued_by='test')
                loaded = runtime.skills.activate_skill(target, 'cross-load-skill', actor=actor)
                assert loaded['pid'] == target
                assert 'echo' in runtime.process.get(target).tool_table
            finally:
                runtime.close()

    def test_cross_process_failed_activation_restores_execute_and_admin_one_shots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(Path(temp_dir), 'cross-fail-skill', allowed_tools=['missing_cross_tool'])
            runtime = Runtime.open('local')
            try:
                actor = runtime.process.spawn(image='base-agent:v0', goal='actor')
                target = runtime.process.spawn(image='base-agent:v0', goal='target')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                execute_cap = runtime.capability.grant_once(
                    actor,
                    'skill:cross-fail-skill',
                    [CapabilityRight.EXECUTE],
                    issued_by='test',
                )
                admin_cap = runtime.capability.grant_once(
                    actor,
                    f'process:{target}',
                    [CapabilityRight.ADMIN],
                    issued_by='test',
                )

                with pytest.raises(NotFound, match='tool not found'):
                    runtime.skills.activate_skill(target, 'cross-fail-skill', actor=actor)

                assert runtime.store.get_capability(execute_cap.cap_id).uses_remaining == 1
                assert runtime.store.get_capability(admin_cap.cap_id).uses_remaining == 1
                assert 'cross-fail-skill' not in runtime.process.get(target).loaded_skills
            finally:
                runtime.close()

    def test_unload_skill_consumes_one_time_execute_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(Path(temp_dir), 'unload-skill', allowed_tools=['echo'])
            runtime = Runtime.open('local')
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='unload skill')
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                runtime.activate_skill(pid, 'unload-skill')
                runtime.capability.grant_once(pid, 'skill:unload-skill', [CapabilityRight.EXECUTE], issued_by='test')
                runtime.skills.unload_skill(pid, 'unload-skill', actor=pid)
                assert not runtime.capability.check(pid, 'skill:unload-skill', CapabilityRight.EXECUTE)
                assert 'echo' not in runtime.process.get(pid).tool_table
            finally:
                runtime.close()

    def test_unload_skill_restores_same_tool_from_process_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = write_skill_package(Path(temp_dir), 'image-shared-skill', allowed_tools=['echo'])
            runtime = Runtime.open('local')
            try:
                image_id = 'skill-shared-image:v0'
                runtime.register_image(
                    AgentImage(
                        image_id=image_id,
                        name='skill-shared-image',
                        default_tools=['echo'],
                    ),
                    actor='test',
                )
                runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
                pid = runtime.process.spawn(image=image_id, goal='preserve image tool after skill unload')
                before_tool_table = dict(runtime.process.get(pid).tool_table)
                before_model_tool_table = dict(runtime.process.get(pid).model_tool_table)

                runtime.activate_skill(pid, 'image-shared-skill')
                runtime.unload_skill(pid, 'image-shared-skill')

                restored = runtime.process.get(pid)
                assert restored.tool_table == before_tool_table
                assert restored.model_tool_table == before_model_tool_table
            finally:
                runtime.close()

    def test_unload_one_of_two_skills_keeps_shared_tool_until_last_source_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_dir = write_skill_package(root, 'shared-tool-first', allowed_tools=['echo'])
            second_dir = write_skill_package(root, 'shared-tool-second', allowed_tools=['echo'])
            runtime = Runtime.open('local')
            try:
                runtime.skills.register_skill_from_path(first_dir, actor='cli', require_capability=False)
                runtime.skills.register_skill_from_path(second_dir, actor='cli', require_capability=False)
                pid = runtime.process.spawn(image='base-agent:v0', goal='shared skill tool provenance')
                runtime.activate_skill(pid, 'shared-tool-first')
                runtime.activate_skill(pid, 'shared-tool-second')

                runtime.unload_skill(pid, 'shared-tool-first')

                after_first_unload = runtime.process.get(pid)
                assert 'shared-tool-second' in after_first_unload.loaded_skills
                assert 'echo' in after_first_unload.tool_table
                assert 'echo' in after_first_unload.model_tool_table

                runtime.unload_skill(pid, 'shared-tool-second')
                after_last_unload = runtime.process.get(pid)
                assert 'echo' not in after_last_unload.tool_table
                assert 'echo' not in after_last_unload.model_tool_table
            finally:
                runtime.close()

    @pytest.mark.parametrize("base_source", ["image", "manual"])
    def test_unload_rejects_noncanonical_persisted_skill_provenance(
        self,
        tmp_path: Path,
        base_source: str,
    ) -> None:
        skill_dir = write_skill_package(tmp_path, f'legacy-{base_source}-skill', allowed_tools=['echo'])
        database = tmp_path / f'legacy-{base_source}-skill.sqlite'
        runtime = Runtime.open(database)
        try:
            image_id = 'base-agent:v0'
            if base_source == 'image':
                image_id = 'legacy-skill-base-image:v0'
                runtime.register_image(
                    AgentImage(
                        image_id=image_id,
                        name='legacy-skill-base-image',
                        default_tools=['echo'],
                    ),
                    actor='test',
                )
            runtime.skills.register_skill_from_path(skill_dir, actor='cli', require_capability=False)
            pid = runtime.process.spawn(image=image_id, goal='strict Skill provenance')
            if base_source == 'manual':
                runtime.tools.configure_process_tools(pid, ['echo'], assigned_by='test:manual-base')
            runtime.activate_skill(pid, f'legacy-{base_source}-skill')
            process = runtime.process.get(pid)
            loaded = dict(process.loaded_skills[f'legacy-{base_source}-skill'])
            loaded.pop('base_tool_ids', None)
            loaded.pop('base_model_tool_ids', None)
            process.loaded_skills[f'legacy-{base_source}-skill'] = loaded
            runtime.store.update_process(process)
        finally:
            runtime.close()

        reopened = Runtime.open(database)
        try:
            with pytest.raises(ValidationError, match='canonical tool provenance'):
                reopened.unload_skill(pid, f'legacy-{base_source}-skill')

            process = reopened.process.get(pid)
            assert f'legacy-{base_source}-skill' in process.loaded_skills
            assert 'echo' in process.tool_table
            assert 'echo' in process.model_tool_table
        finally:
            reopened.close()

    def _run(self, awaitable: Any) -> Any:
        return asyncio.run(awaitable)

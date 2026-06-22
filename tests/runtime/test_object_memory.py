from __future__ import annotations
import pytest
import json
import sqlite3
import tempfile
from dataclasses import replace
from agent_libos import Runtime
from agent_libos.models.exceptions import CapabilityDenied, NotFound, ValidationError
from agent_libos.models import CapabilityRight, MemoryViewSpec, ObjectFilter, ObjectHandle, ObjectMetadata, ObjectPatch, ObjectQuery, ObjectType

class TestObjectMemoryName:

    def setup_method(self) -> None:
        self.runtime = Runtime.open('local')

    def teardown_method(self) -> None:
        self.runtime.close()

    def test_object_has_unique_name_and_can_be_read_by_name_with_permission(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='name access')
        handle = self.runtime.memory.create_object(pid=pid, object_type=ObjectType.PLAN, payload={'steps': ['inspect', 'patch']}, name='repo.plan')
        obj = self.runtime.memory.get_object(pid, handle)
        by_name = self.runtime.memory.get_object_by_name(pid, 'repo.plan')
        handle_by_name = self.runtime.memory.handle_for_name(pid, 'repo.plan')
        assert obj.name == 'repo.plan'
        assert obj.namespace == self.runtime.memory.resolve_namespace(pid)
        assert by_name.oid == handle.oid
        assert handle_by_name.oid == handle.oid
        assert 'read' in handle_by_name.rights

    def test_duplicate_object_name_is_rejected(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='duplicate names')
        self.runtime.memory.create_object(pid=pid, object_type=ObjectType.OBSERVATION, payload={'value': 1}, name='duplicate.name')
        with pytest.raises(ValidationError):
            self.runtime.memory.create_object(pid=pid, object_type=ObjectType.OBSERVATION, payload={'value': 2}, name='duplicate.name')

    def test_object_payload_rolls_back_when_sql_insert_fails(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='atomic object insert')
        handle = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.OBSERVATION,
            payload={'value': 'original'},
            name='atomic.insert',
        )
        original = self.runtime.store.get_object(handle.oid)
        duplicate = original.__class__(**{**original.__dict__, 'payload': {'value': 'bad'}})

        with pytest.raises(sqlite3.IntegrityError):
            self.runtime.store.insert_object(duplicate)

        assert self.runtime.store.object_payload(handle.oid) == {'value': 'original'}

    def test_object_memory_payload_limits_reject_create_update_and_append_without_partial_write(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='memory limits')
        oversized_payload = {'blob': 'x' * self.runtime.config.tools.memory_payload_hard_limit_bytes}
        with pytest.raises(ValidationError):
            self.runtime.memory.create_object(
                pid=pid,
                object_type=ObjectType.OBSERVATION,
                payload=oversized_payload,
                name='too.large',
            )

        handle = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.OBSERVATION,
            payload={'entries': []},
            name='append.log',
            immutable=False,
        )
        with pytest.raises(ValidationError):
            self.runtime.memory.update_object(pid, handle, ObjectPatch(payload=oversized_payload))
        assert self.runtime.memory.get_object(pid, handle).payload == {'entries': []}

        oversized_entry = {'blob': 'x' * self.runtime.config.tools.memory_append_entry_max_bytes}
        appended = self.runtime.tools.call(pid, 'append_memory_object', {'name': 'append.log', 'entry': oversized_entry})
        assert not appended.ok
        assert 'memory append entry exceeds' in (appended.error or '')
        assert self.runtime.memory.get_object(pid, handle).payload == {'entries': []}

    def test_mutable_payload_update_refreshes_token_estimate_for_materialization_budget(self) -> None:
        sentinel = 'UPDATED_MEMORY_BUDGET_SENTINEL'
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='memory budget update')
        handle = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.OBSERVATION,
            payload={'entries': []},
            name='budget.update',
            immutable=False,
        )
        original = self.runtime.memory.get_object(pid, handle)

        self.runtime.memory.update_object(
            pid,
            handle,
            ObjectPatch(payload={'entries': [(sentinel + ' ') * 200]}),
        )

        updated = self.runtime.memory.get_object(pid, handle)
        assert updated.metadata.token_estimate is not None
        assert original.metadata.token_estimate is not None
        assert updated.metadata.token_estimate > original.metadata.token_estimate
        view = self.runtime.memory.create_view(pid, [handle])
        context = self.runtime.memory.materialize_context(pid, view, budget_tokens=4)
        assert handle.oid in context.omitted_objects
        assert sentinel not in context.text

    def test_append_refreshes_token_estimate_for_materialization_budget(self) -> None:
        sentinel = 'APPENDED_MEMORY_BUDGET_SENTINEL'
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='memory budget append')
        handle = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.OBSERVATION,
            payload={'entries': []},
            name='budget.append',
            immutable=False,
        )

        appended = self.runtime.tools.call(
            pid,
            'append_memory_object',
            {'name': 'budget.append', 'entry': {'text': (sentinel + ' ') * 200}},
        )

        assert appended.ok, appended.error
        updated = self.runtime.memory.get_object(pid, handle)
        assert updated.metadata.token_estimate is not None
        assert updated.metadata.token_estimate > 4
        view = self.runtime.memory.create_view(pid, [handle])
        context = self.runtime.memory.materialize_context(pid, view, budget_tokens=4)
        assert handle.oid in context.omitted_objects
        assert sentinel not in context.text

    def test_object_patch_distinguishes_unset_payload_from_json_null(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='patch null')
        handle = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.OBSERVATION,
            payload={'value': 1},
            name='patch.null',
            immutable=False,
        )

        self.runtime.memory.update_object(pid, handle, ObjectPatch(name='patch.null.renamed'))
        assert self.runtime.memory.get_object(pid, handle).payload == {'value': 1}

        self.runtime.memory.update_object(pid, handle, ObjectPatch(payload=None))
        assert self.runtime.memory.get_object(pid, handle).payload is None

    def test_memory_view_filters_apply_before_context_materialization(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='filtered context')
        tagged = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.EVIDENCE,
            payload={'text': 'include this needle'},
            metadata=ObjectMetadata(tags=['keep']),
            name='filter.tagged',
        )
        typed = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.SUMMARY,
            payload={'text': 'include this summary'},
            name='filter.typed',
        )
        omitted = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.PLAN,
            payload={'text': 'do not include this'},
            name='filter.omitted',
        )
        view = self.runtime.memory.create_view(
            pid,
            [tagged, typed, omitted],
            filters=[
                ObjectFilter(tags=['keep'], text='needle'),
                ObjectFilter(type=ObjectType.SUMMARY),
            ],
        )

        context = self.runtime.memory.materialize_context(pid, view)

        assert tagged.oid in context.object_refs
        assert typed.oid in context.object_refs
        assert omitted.oid in context.omitted_objects
        assert 'include this needle' in context.text
        assert 'include this summary' in context.text
        assert 'do not include this' not in context.text

    @pytest.mark.parametrize('name', ('project/note', 'project\\note', '.', '..'))
    def test_object_name_cannot_contain_namespace_separators(self, name: str) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='invalid local names')
        with pytest.raises(ValidationError):
            self.runtime.memory.create_object(pid=pid, object_type=ObjectType.OBSERVATION, payload={'name': name}, name=name)

    def test_same_local_name_is_allowed_in_process_and_explicit_namespaces(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='namespace names')
        self.runtime.memory.create_namespace(pid, 'project')
        process_handle = self.runtime.memory.create_object(pid=pid, object_type=ObjectType.ARTIFACT, payload={'scope': 'process'}, name='shared.name')
        project_handle = self.runtime.memory.create_object(pid=pid, object_type=ObjectType.ARTIFACT, payload={'scope': 'project'}, name='shared.name', namespace='project')
        process_obj = self.runtime.memory.get_object_by_name(pid, 'shared.name')
        project_obj = self.runtime.memory.get_object_by_name(pid, 'shared.name', namespace='project')
        project_results = self.runtime.memory.query_objects(pid, ObjectQuery(name='shared.name', namespace='project'))
        default_results = self.runtime.memory.query_objects(pid, ObjectQuery(name='shared.name'))
        assert process_obj.oid == process_handle.oid
        assert project_obj.oid == project_handle.oid
        assert project_obj.namespace == 'project'
        assert [handle.oid for handle in project_results] == [project_handle.oid]
        assert [handle.oid for handle in default_results] == [process_handle.oid]
        with pytest.raises(ValidationError):
            self.runtime.memory.create_object(pid=pid, object_type=ObjectType.ARTIFACT, payload={'duplicate': True}, name='shared.name', namespace='project')

    def test_same_bare_name_is_isolated_between_process_namespaces(self) -> None:
        first = self.runtime.process.spawn(image='base-agent:v0', goal='first namespace')
        second = self.runtime.process.spawn(image='base-agent:v0', goal='second namespace')
        first_handle = self.runtime.memory.create_object(pid=first, object_type=ObjectType.OBSERVATION, payload={'owner': 'first'}, name='local.note')
        second_handle = self.runtime.memory.create_object(pid=second, object_type=ObjectType.OBSERVATION, payload={'owner': 'second'}, name='local.note')
        assert first_handle.oid != second_handle.oid
        assert self.runtime.memory.get_object_by_name(first, 'local.note').oid == first_handle.oid
        assert self.runtime.memory.get_object_by_name(second, 'local.note').oid == second_handle.oid

    def test_namespace_write_and_list_rights_are_enforced(self) -> None:
        owner = self.runtime.process.spawn(image='base-agent:v0', goal='owner namespace')
        other = self.runtime.process.spawn(image='base-agent:v0', goal='other namespace')
        self.runtime.memory.create_namespace(owner, 'private')
        handle = self.runtime.memory.create_object(pid=owner, object_type=ObjectType.EVIDENCE, payload={'secret': 'namespaced'}, name='evidence', namespace='private')
        with pytest.raises(CapabilityDenied):
            self.runtime.memory.create_object(pid=other, object_type=ObjectType.EVIDENCE, payload={'write': 'denied'}, name='evidence', namespace='private')
        with pytest.raises(CapabilityDenied):
            self.runtime.memory.get_object_by_name(other, 'evidence', namespace='private')
        with pytest.raises(CapabilityDenied):
            self.runtime.memory.get_object_by_name(other, 'missing', namespace='private')
        with pytest.raises(CapabilityDenied):
            self.runtime.memory.handle_for_name(other, 'missing', namespace='private')
        self.runtime.capability.grant(subject=other, resource=f'object:{handle.oid}', rights=[CapabilityRight.READ], issued_by='test')
        with pytest.raises(CapabilityDenied):
            self.runtime.memory.get_object_by_name(other, 'evidence', namespace='private')
        with pytest.raises(CapabilityDenied):
            self.runtime.memory.list_namespace(other, 'private')
        self.runtime.capability.grant(subject=other, resource='object_namespace:private', rights=['read'], issued_by='test')
        obj = self.runtime.memory.get_object_by_name(other, 'evidence', namespace='private')
        listing = self.runtime.memory.list_namespace(other, 'private')
        assert obj.payload == {'secret': 'namespaced'}
        assert [obj.name for obj in listing['objects']] == ['evidence']

    def test_one_time_namespace_read_grant_is_consumed_after_successful_lookup(self) -> None:
        owner = self.runtime.process.spawn(image='base-agent:v0', goal='namespace read owner')
        reader = self.runtime.process.spawn(image='base-agent:v0', goal='namespace read once')
        namespace = 'shared-read-once'
        self.runtime.memory.create_namespace(owner, namespace)
        handle = self.runtime.memory.create_object(
            pid=owner,
            object_type=ObjectType.EVIDENCE,
            payload={'secret': 'single directory lookup'},
            name='evidence',
            namespace=namespace,
        )
        self.runtime.capability.grant(
            subject=reader,
            resource=f'object:{handle.oid}',
            rights=[CapabilityRight.READ],
            issued_by='test',
        )
        namespace_cap = self.runtime.capability.grant_once(
            reader,
            f'object_namespace:{namespace}',
            ['read'],
            issued_by='test',
        )

        with pytest.raises(NotFound):
            self.runtime.memory.get_object_by_name(reader, 'missing', namespace=namespace)
        assert self.runtime.store.get_capability(namespace_cap.cap_id).active

        obj = self.runtime.memory.get_object_by_name(reader, 'evidence', namespace=namespace)

        assert obj.oid == handle.oid
        assert not self.runtime.store.get_capability(namespace_cap.cap_id).active
        with pytest.raises(CapabilityDenied):
            self.runtime.memory.get_object_by_name(reader, 'evidence', namespace=namespace)

    def test_one_time_namespace_write_grant_is_consumed_after_successful_create(self) -> None:
        owner = self.runtime.process.spawn(image='base-agent:v0', goal='namespace write owner')
        writer = self.runtime.process.spawn(image='base-agent:v0', goal='namespace write once')
        namespace = 'shared-write-once'
        self.runtime.memory.create_namespace(owner, namespace)
        self.runtime.memory.create_object(
            pid=owner,
            object_type=ObjectType.OBSERVATION,
            payload={'owned': True},
            name='existing',
            namespace=namespace,
        )
        namespace_cap = self.runtime.capability.grant_once(
            writer,
            f'object_namespace:{namespace}',
            ['write'],
            issued_by='test',
        )

        with pytest.raises(ValidationError):
            self.runtime.memory.create_object(
                pid=writer,
                object_type=ObjectType.OBSERVATION,
                payload={'duplicate': True},
                name='existing',
                namespace=namespace,
            )
        assert self.runtime.store.get_capability(namespace_cap.cap_id).active

        created = self.runtime.memory.create_object(
            pid=writer,
            object_type=ObjectType.OBSERVATION,
            payload={'created': 1},
            name='first',
            namespace=namespace,
        )

        assert created.oid
        assert not self.runtime.store.get_capability(namespace_cap.cap_id).active
        with pytest.raises(CapabilityDenied):
            self.runtime.memory.create_object(
                pid=writer,
                object_type=ObjectType.OBSERVATION,
                payload={'created': 2},
                name='second',
                namespace=namespace,
            )

    def test_mutable_object_can_move_between_namespaces_when_target_name_is_unique(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='move namespace')
        self.runtime.memory.create_namespace(pid, 'drafts')
        self.runtime.memory.create_namespace(pid, 'final')
        handle = self.runtime.memory.create_object(pid=pid, object_type=ObjectType.ARTIFACT, payload={'value': 'draft'}, name='report', namespace='drafts', immutable=False)
        self.runtime.memory.create_object(pid=pid, object_type=ObjectType.ARTIFACT, payload={'value': 'existing'}, name='report', namespace='final')
        with pytest.raises(ValidationError):
            self.runtime.memory.update_object(pid, handle, ObjectPatch(namespace='final'))
        self.runtime.memory.update_object(pid, handle, ObjectPatch(namespace='final', name='report.v2'))
        with pytest.raises(NotFound):
            self.runtime.memory.get_object_by_name(pid, 'report', namespace='drafts')
        moved = self.runtime.memory.get_object_by_name(pid, 'report.v2', namespace='final')
        assert moved.oid == handle.oid

    def test_namespace_tools_create_objects_and_list_visible_entries(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='namespace tools')
        created_ns = self.runtime.tools.call(pid, 'create_memory_namespace', {'namespace': 'toolspace'})
        created_obj = self.runtime.tools.call(pid, 'create_memory_object', {'namespace': 'toolspace', 'name': 'note', 'type': 'summary', 'payload': {'ok': True}})
        listed = self.runtime.tools.call(pid, 'list_memory_namespace', {'namespace': 'toolspace'})
        listed_default = self.runtime.tools.call(pid, 'list_memory_namespace', {})
        assert created_ns.ok, created_ns.error
        assert created_obj.ok, created_obj.error
        assert created_obj.payload['namespace'] == 'toolspace'
        assert listed.ok, listed.error
        assert listed.payload['objects'][0]['name'] == 'note'
        assert listed_default.ok, listed_default.error
        assert listed_default.payload['namespace'] == self.runtime.memory.resolve_namespace(pid)

    def test_name_lookup_does_not_bypass_object_capability(self) -> None:
        owner = self.runtime.process.spawn(image='base-agent:v0', goal='owner')
        other = self.runtime.process.spawn(image='base-agent:v0', goal='other')
        handle = self.runtime.memory.create_object(pid=owner, object_type=ObjectType.EVIDENCE, payload={'secret': 'owner-only'}, name='private.evidence')
        with pytest.raises(NotFound):
            self.runtime.memory.get_object_by_name(other, 'private.evidence')
        owner_namespace = self.runtime.memory.resolve_namespace(owner)
        self.runtime.capability.grant(subject=other, resource=f'object:{handle.oid}', rights=[CapabilityRight.READ], issued_by='test')
        with pytest.raises(CapabilityDenied):
            self.runtime.memory.get_object_by_name(other, 'private.evidence', namespace=owner_namespace)
        self.runtime.capability.grant(subject=other, resource=f'object_namespace:{owner_namespace}', rights=['read'], issued_by='test')
        obj = self.runtime.memory.get_object_by_name(other, 'private.evidence', namespace=owner_namespace)
        assert obj.payload == {'secret': 'owner-only'}
        with pytest.raises(CapabilityDenied):
            self.runtime.memory.handle_for_name(other, 'private.evidence', rights=['write'], namespace=owner_namespace)

    def test_one_time_object_name_lookup_does_not_become_persistent_handle(self) -> None:
        owner = self.runtime.process.spawn(image='base-agent:v0', goal='owner one-shot')
        by_name_reader = self.runtime.process.spawn(image='base-agent:v0', goal='reader by name')
        handle_reader = self.runtime.process.spawn(image='base-agent:v0', goal='reader handle')
        handle = self.runtime.memory.create_object(pid=owner, object_type=ObjectType.EVIDENCE, payload={'secret': 'read once'}, name='one.shot')
        owner_namespace = self.runtime.memory.resolve_namespace(owner)
        for pid in [by_name_reader, handle_reader]:
            self.runtime.capability.grant(subject=pid, resource=f'object_namespace:{owner_namespace}', rights=['read'], issued_by='test')
        self.runtime.capability.grant_once(by_name_reader, f'object:{handle.oid}', [CapabilityRight.READ], issued_by='test')
        obj = self.runtime.memory.get_object_by_name(by_name_reader, 'one.shot', namespace=owner_namespace)
        assert obj.payload == {'secret': 'read once'}
        with pytest.raises(CapabilityDenied):
            self.runtime.memory.get_object_by_name(by_name_reader, 'one.shot', namespace=owner_namespace)
        source_cap = self.runtime.capability.grant_once(handle_reader, f'object:{handle.oid}', [CapabilityRight.READ], issued_by='test')
        one_shot_handle = self.runtime.memory.handle_for_name(handle_reader, 'one.shot', namespace=owner_namespace)
        assert not self.runtime.store.get_capability(source_cap.cap_id).active
        assert self.runtime.memory.get_object(handle_reader, one_shot_handle).payload == {'secret': 'read once'}
        with pytest.raises(CapabilityDenied):
            self.runtime.memory.get_object(handle_reader, one_shot_handle)

    def test_one_time_multi_right_name_handle_consumes_source_capability_once(self) -> None:
        owner = self.runtime.process.spawn(image='base-agent:v0', goal='owner multi-right')
        reader = self.runtime.process.spawn(image='base-agent:v0', goal='reader multi-right')
        handle = self.runtime.memory.create_object(
            pid=owner,
            object_type=ObjectType.OBSERVATION,
            payload={'entries': []},
            name='one.shot.multi',
            immutable=False,
        )
        owner_namespace = self.runtime.memory.resolve_namespace(owner)
        self.runtime.capability.grant(subject=reader, resource=f'object_namespace:{owner_namespace}', rights=['read'], issued_by='test')
        source_cap = self.runtime.capability.grant_once(
            reader,
            f'object:{handle.oid}',
            [CapabilityRight.READ, CapabilityRight.WRITE],
            issued_by='test',
        )

        one_shot_handle = self.runtime.memory.handle_for_name(
            reader,
            'one.shot.multi',
            rights=['read', 'write'],
            namespace=owner_namespace,
        )

        consumes = [
            record
            for record in self.runtime.audit.trace()
            if record.action == 'capability.consume' and source_cap.cap_id in record.capability_refs
        ]
        assert one_shot_handle.rights == {'read', 'write'}
        assert len(consumes) == 1
        assert not self.runtime.store.get_capability(source_cap.cap_id).active

    def test_one_time_read_write_grant_allows_single_append_operation(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='one-shot append')
        handle = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.OBSERVATION,
            payload={'entries': []},
            name='one.shot.append',
            immutable=False,
        )
        for cap in self.runtime.capability.capabilities_for(pid):
            if cap.resource == f'object:{handle.oid}':
                self.runtime.capability.revoke(cap.cap_id, revoked_by='test', require_authority=False)
        source_cap = self.runtime.capability.grant_once(
            pid,
            f'object:{handle.oid}',
            [CapabilityRight.READ, CapabilityRight.WRITE],
            issued_by='test',
        )

        appended = self.runtime.tools.call(pid, 'append_memory_object', {'name': 'one.shot.append', 'entry': {'x': 1}})

        assert appended.ok, appended.error
        assert not self.runtime.store.get_capability(source_cap.cap_id).active
        assert self.runtime.store.get_object(handle.oid).payload == {'entries': [{'x': 1}]}
        second = self.runtime.tools.call(pid, 'append_memory_object', {'name': 'one.shot.append', 'entry': {'x': 2}})
        assert not second.ok

    def test_query_by_name_only_returns_accessible_objects(self) -> None:
        owner = self.runtime.process.spawn(image='base-agent:v0', goal='owner query')
        other = self.runtime.process.spawn(image='base-agent:v0', goal='other query')
        handle = self.runtime.memory.create_object(pid=owner, object_type=ObjectType.CLAIM, payload={'claim': 'name lookup is capability checked'}, name='claim.capability')
        assert self.runtime.memory.query_objects(other, ObjectQuery(name='claim.capability')) == []
        self.runtime.capability.grant(subject=other, resource=f'object:{handle.oid}', rights=[CapabilityRight.READ], issued_by='test')
        owner_namespace = self.runtime.memory.resolve_namespace(owner)
        with pytest.raises(CapabilityDenied):
            self.runtime.memory.query_objects(other, ObjectQuery(name='claim.capability', namespace=owner_namespace))
        self.runtime.capability.grant(subject=other, resource=f'object_namespace:{owner_namespace}', rights=['read'], issued_by='test')
        results = self.runtime.memory.query_objects(other, ObjectQuery(name='claim.capability', namespace=owner_namespace))
        assert len(results) == 1
        assert results[0].oid == handle.oid

    def test_query_limit_uses_deterministic_recent_first_order(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='ordered query')
        older = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.OBSERVATION,
            payload={'rank': 'older'},
            name='ordered.older',
        )
        newer = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.OBSERVATION,
            payload={'rank': 'newer'},
            name='ordered.newer',
        )
        self.runtime.store.update_object(replace(self.runtime.store.get_object(older.oid), updated_at='2026-01-01T00:00:00Z'))
        self.runtime.store.update_object(replace(self.runtime.store.get_object(newer.oid), updated_at='2026-01-02T00:00:00Z'))

        results = self.runtime.memory.query_objects(pid, ObjectQuery(type=ObjectType.OBSERVATION, limit=1))

        assert [handle.oid for handle in results] == [newer.oid]

    def test_query_with_read_only_authority_does_not_grant_materialize_or_link(self) -> None:
        owner = self.runtime.process.spawn(image='base-agent:v0', goal='owner query materialize')
        reader = self.runtime.process.spawn(image='base-agent:v0', goal='reader query materialize')
        handle = self.runtime.memory.create_object(
            pid=owner,
            object_type=ObjectType.EVIDENCE,
            payload={'secret': 'query must not materialize this'},
            name='query.secret',
        )
        owner_namespace = self.runtime.memory.resolve_namespace(owner)
        self.runtime.capability.grant(subject=reader, resource=f'object_namespace:{owner_namespace}', rights=['read'], issued_by='test')
        self.runtime.capability.grant(subject=reader, resource=f'object:{handle.oid}', rights=[CapabilityRight.READ], issued_by='test')

        results = self.runtime.memory.query_objects(reader, ObjectQuery(name='query.secret', namespace=owner_namespace))

        assert len(results) == 1
        assert results[0].rights == {'read'}
        view = self.runtime.memory.create_view(reader, results)
        context = self.runtime.memory.materialize_context(reader, view)
        assert handle.oid in context.omitted_objects
        assert 'query must not materialize this' not in context.text

    @pytest.mark.parametrize('limit', (0, -1))
    def test_query_limit_is_validated_before_scan(self, limit: int) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='query limits')
        self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.EVIDENCE,
            payload={'visible': True},
            name=f'limit.{limit}',
        )

        with pytest.raises(ValidationError):
            self.runtime.memory.query_objects(pid, ObjectQuery(limit=limit))
        with pytest.raises(ValidationError):
            self.runtime.memory.query_objects(pid, ObjectQuery(limit=self.runtime.config.memory.query_limit + 1))

    def test_fork_view_explicit_rights_cannot_exceed_parent_handle(self) -> None:
        owner = self.runtime.process.spawn(image='base-agent:v0', goal='owner fork rights')
        parent = self.runtime.process.spawn(image='base-agent:v0', goal='parent fork rights')
        handle = self.runtime.memory.create_object(
            pid=owner,
            object_type=ObjectType.EVIDENCE,
            payload={'secret': 'read only'},
            name='fork.secret',
        )
        owner_namespace = self.runtime.memory.resolve_namespace(owner)
        self.runtime.capability.grant(subject=parent, resource=f'object_namespace:{owner_namespace}', rights=['read'], issued_by='test')
        self.runtime.capability.grant(subject=parent, resource=f'object:{handle.oid}', rights=[CapabilityRight.READ], issued_by='test')
        read_only = self.runtime.memory.handle_for_name(
            parent,
            'fork.secret',
            rights=['read'],
            namespace=owner_namespace,
        )
        parent_view = self.runtime.memory.create_view(parent, [read_only])

        with pytest.raises(CapabilityDenied):
            self.runtime.memory.fork_view(
                parent,
                'pid_fake_child',
                parent_view,
                MemoryViewSpec(roots=[read_only], rights={'read', 'materialize'}),
            )

    def test_object_handle_capability_cannot_be_retargeted_to_another_oid(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='retarget handle')
        first = self.runtime.memory.create_object(pid=pid, object_type=ObjectType.EVIDENCE, payload={'first': True}, name='first')
        second = self.runtime.memory.create_object(pid=pid, object_type=ObjectType.EVIDENCE, payload={'second': True}, name='second')
        forged = ObjectHandle(oid=second.oid, rights=set(first.rights), capability_id=first.capability_id)

        with pytest.raises(CapabilityDenied):
            self.runtime.memory.get_object(pid, forged)

    def test_mutable_object_can_be_renamed_with_unique_name(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='rename')
        handle = self.runtime.memory.create_object(pid=pid, object_type=ObjectType.ARTIFACT, payload={'value': 'draft'}, name='artifact.old', immutable=False)
        self.runtime.memory.create_object(pid=pid, object_type=ObjectType.ARTIFACT, payload={'value': 'other'}, name='artifact.other')
        self.runtime.memory.update_object(pid, handle, ObjectPatch(name='artifact.new'))
        with pytest.raises(NotFound):
            self.runtime.memory.get_object_by_name(pid, 'artifact.old')
        assert self.runtime.memory.get_object_by_name(pid, 'artifact.new').oid == handle.oid
        with pytest.raises(ValidationError):
            self.runtime.memory.update_object(pid, handle, ObjectPatch(name='artifact.other'))

    def test_object_payload_is_not_written_to_sqlite(self) -> None:
        self.runtime.close()
        secret = 'SECRET_MEMORY_PAYLOAD_SHOULD_NOT_BE_IN_SQL'
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = f'{temp_dir}/runtime.sqlite'
            runtime = Runtime.open(db_path)
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='sqlite payload boundary')
                handle = runtime.memory.create_object(pid=pid, object_type=ObjectType.ARTIFACT, payload={'secret': secret}, name='volatile.secret')
                assert runtime.memory.get_object(pid, handle).payload == {'secret': secret}
            finally:
                runtime.close()
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute('SELECT payload_json FROM objects').fetchall()
            finally:
                conn.close()
            serialized = json.dumps(rows)
        self.runtime = Runtime.open('local')
        assert secret not in serialized
        assert 'runtime_memory' in serialized

    def test_stale_persistent_process_name_does_not_block_new_process_namespace(self) -> None:
        self.runtime.close()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = f'{temp_dir}/runtime.sqlite'
            runtime = Runtime.open(db_path)
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='reserve name')
                runtime.memory.create_object(pid=pid, object_type=ObjectType.ARTIFACT, payload={'runtime_only': True}, name='reserved.name')
            finally:
                runtime.close()
            reopened = Runtime.open(db_path)
            try:
                pid = reopened.process.spawn(image='base-agent:v0', goal='duplicate stale name')
                handle = reopened.memory.create_object(pid=pid, object_type=ObjectType.ARTIFACT, payload={'new': True}, name='reserved.name')
                obj = reopened.memory.get_object(pid, handle)
                assert obj.namespace == reopened.memory.resolve_namespace(pid)
            finally:
                reopened.close()
        self.runtime = Runtime.open('local')

    def test_legacy_name_only_schema_does_not_block_process_namespace(self) -> None:
        self.runtime.close()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = f'{temp_dir}/legacy.sqlite'
            conn = sqlite3.connect(db_path)
            try:
                conn.execute('\n                    CREATE TABLE objects (\n                      oid TEXT PRIMARY KEY,\n                      name TEXT NOT NULL UNIQUE,\n                      type TEXT NOT NULL,\n                      schema_version TEXT NOT NULL,\n                      payload_json TEXT NOT NULL,\n                      metadata_json TEXT NOT NULL,\n                      provenance_json TEXT NOT NULL,\n                      version INTEGER NOT NULL,\n                      immutable INTEGER NOT NULL,\n                      created_by TEXT NOT NULL,\n                      created_at TEXT NOT NULL,\n                      updated_at TEXT NOT NULL\n                    )\n                    ')
                conn.execute('INSERT INTO objects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', ('obj_legacy', 'same.local', 'artifact', '1', '{}', '{}', '{}', 1, 1, 'legacy', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'))
                conn.commit()
            finally:
                conn.close()
            runtime = Runtime.open(db_path)
            try:
                pid = runtime.process.spawn(image='base-agent:v0', goal='legacy migration')
                runtime.memory.create_namespace(pid, 'legacy')
                handle = runtime.memory.create_object(pid=pid, object_type=ObjectType.ARTIFACT, payload={'namespaced': True}, name='same.local', namespace='legacy')
                assert runtime.memory.get_object(pid, handle).namespace == 'legacy'
                process_handle = runtime.memory.create_object(pid=pid, object_type=ObjectType.ARTIFACT, payload={'process': True}, name='same.local')
                assert runtime.memory.get_object(pid, process_handle).namespace == runtime.memory.resolve_namespace(pid)
            finally:
                runtime.close()
        self.runtime = Runtime.open('local')

    def test_process_exit_releases_owned_memory_except_result_object(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='release memory')
        scratch = self.runtime.memory.create_object(pid=pid, object_type=ObjectType.OBSERVATION, payload={'temporary': True}, name='scratch.memory')
        result = self.runtime.memory.create_object(pid=pid, object_type=ObjectType.SUMMARY, payload={'kept': True}, name='result.memory')
        self.runtime.process.exit(pid, result=result)
        assert self.runtime.store.get_object(scratch.oid) is None
        assert self.runtime.store.get_object(result.oid) is not None
        assert self.runtime.store.get_object(result.oid).payload == {'kept': True}
        with pytest.raises(NotFound):
            self.runtime.memory.get_object_by_name(pid, 'scratch.memory')

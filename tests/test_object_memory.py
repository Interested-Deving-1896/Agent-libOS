from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest

from agent_libos import Runtime
from agent_libos.models.exceptions import CapabilityDenied, NotFound, ValidationError
from agent_libos.models import CapabilityRight, ObjectPatch, ObjectQuery, ObjectType


class ObjectMemoryNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Runtime.open("local")

    def tearDown(self) -> None:
        self.runtime.close()

    def test_object_has_unique_name_and_can_be_read_by_name_with_permission(self) -> None:
        pid = self.runtime.process.spawn(image="base-agent:v0", goal="name access")
        handle = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.PLAN,
            payload={"steps": ["inspect", "patch"]},
            name="repo.plan",
        )

        obj = self.runtime.memory.get_object(pid, handle)
        by_name = self.runtime.memory.get_object_by_name(pid, "repo.plan")
        handle_by_name = self.runtime.memory.handle_for_name(pid, "repo.plan")

        self.assertEqual(obj.name, "repo.plan")
        self.assertEqual(obj.namespace, self.runtime.memory.resolve_namespace(pid))
        self.assertEqual(by_name.oid, handle.oid)
        self.assertEqual(handle_by_name.oid, handle.oid)
        self.assertIn("read", handle_by_name.rights)

    def test_duplicate_object_name_is_rejected(self) -> None:
        pid = self.runtime.process.spawn(image="base-agent:v0", goal="duplicate names")
        self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.OBSERVATION,
            payload={"value": 1},
            name="duplicate.name",
        )

        with self.assertRaises(ValidationError):
            self.runtime.memory.create_object(
                pid=pid,
                object_type=ObjectType.OBSERVATION,
                payload={"value": 2},
                name="duplicate.name",
            )

    def test_object_name_cannot_contain_namespace_separators(self) -> None:
        pid = self.runtime.process.spawn(image="base-agent:v0", goal="invalid local names")

        for name in ("project/note", "project\\note", ".", ".."):
            with self.subTest(name=name):
                with self.assertRaises(ValidationError):
                    self.runtime.memory.create_object(
                        pid=pid,
                        object_type=ObjectType.OBSERVATION,
                        payload={"name": name},
                        name=name,
                    )

    def test_same_local_name_is_allowed_in_process_and_explicit_namespaces(self) -> None:
        pid = self.runtime.process.spawn(image="base-agent:v0", goal="namespace names")
        self.runtime.memory.create_namespace(pid, "project")
        process_handle = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.ARTIFACT,
            payload={"scope": "process"},
            name="shared.name",
        )
        project_handle = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.ARTIFACT,
            payload={"scope": "project"},
            name="shared.name",
            namespace="project",
        )

        process_obj = self.runtime.memory.get_object_by_name(pid, "shared.name")
        project_obj = self.runtime.memory.get_object_by_name(pid, "shared.name", namespace="project")
        project_results = self.runtime.memory.query_objects(pid, ObjectQuery(name="shared.name", namespace="project"))
        default_results = self.runtime.memory.query_objects(pid, ObjectQuery(name="shared.name"))

        self.assertEqual(process_obj.oid, process_handle.oid)
        self.assertEqual(project_obj.oid, project_handle.oid)
        self.assertEqual(project_obj.namespace, "project")
        self.assertEqual([handle.oid for handle in project_results], [project_handle.oid])
        self.assertEqual([handle.oid for handle in default_results], [process_handle.oid])

        with self.assertRaises(ValidationError):
            self.runtime.memory.create_object(
                pid=pid,
                object_type=ObjectType.ARTIFACT,
                payload={"duplicate": True},
                name="shared.name",
                namespace="project",
            )

    def test_same_bare_name_is_isolated_between_process_namespaces(self) -> None:
        first = self.runtime.process.spawn(image="base-agent:v0", goal="first namespace")
        second = self.runtime.process.spawn(image="base-agent:v0", goal="second namespace")

        first_handle = self.runtime.memory.create_object(
            pid=first,
            object_type=ObjectType.OBSERVATION,
            payload={"owner": "first"},
            name="local.note",
        )
        second_handle = self.runtime.memory.create_object(
            pid=second,
            object_type=ObjectType.OBSERVATION,
            payload={"owner": "second"},
            name="local.note",
        )

        self.assertNotEqual(first_handle.oid, second_handle.oid)
        self.assertEqual(self.runtime.memory.get_object_by_name(first, "local.note").oid, first_handle.oid)
        self.assertEqual(self.runtime.memory.get_object_by_name(second, "local.note").oid, second_handle.oid)

    def test_namespace_write_and_list_rights_are_enforced(self) -> None:
        owner = self.runtime.process.spawn(image="base-agent:v0", goal="owner namespace")
        other = self.runtime.process.spawn(image="base-agent:v0", goal="other namespace")
        self.runtime.memory.create_namespace(owner, "private")
        handle = self.runtime.memory.create_object(
            pid=owner,
            object_type=ObjectType.EVIDENCE,
            payload={"secret": "namespaced"},
            name="evidence",
            namespace="private",
        )

        with self.assertRaises(CapabilityDenied):
            self.runtime.memory.create_object(
                pid=other,
                object_type=ObjectType.EVIDENCE,
                payload={"write": "denied"},
                name="evidence",
                namespace="private",
            )

        with self.assertRaises(CapabilityDenied):
            self.runtime.memory.get_object_by_name(other, "evidence", namespace="private")

        self.runtime.capability.grant(
            subject=other,
            resource=f"object:{handle.oid}",
            rights=[CapabilityRight.READ],
            issued_by="test",
        )
        with self.assertRaises(CapabilityDenied):
            self.runtime.memory.get_object_by_name(other, "evidence", namespace="private")

        with self.assertRaises(CapabilityDenied):
            self.runtime.memory.list_namespace(other, "private")

        self.runtime.capability.grant(
            subject=other,
            resource="object_namespace:private",
            rights=["read"],
            issued_by="test",
        )
        obj = self.runtime.memory.get_object_by_name(other, "evidence", namespace="private")
        listing = self.runtime.memory.list_namespace(other, "private")
        self.assertEqual(obj.payload, {"secret": "namespaced"})
        self.assertEqual([obj.name for obj in listing["objects"]], ["evidence"])

    def test_mutable_object_can_move_between_namespaces_when_target_name_is_unique(self) -> None:
        pid = self.runtime.process.spawn(image="base-agent:v0", goal="move namespace")
        self.runtime.memory.create_namespace(pid, "drafts")
        self.runtime.memory.create_namespace(pid, "final")
        handle = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.ARTIFACT,
            payload={"value": "draft"},
            name="report",
            namespace="drafts",
            immutable=False,
        )
        self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.ARTIFACT,
            payload={"value": "existing"},
            name="report",
            namespace="final",
        )

        with self.assertRaises(ValidationError):
            self.runtime.memory.update_object(pid, handle, ObjectPatch(namespace="final"))

        self.runtime.memory.update_object(pid, handle, ObjectPatch(namespace="final", name="report.v2"))

        with self.assertRaises(NotFound):
            self.runtime.memory.get_object_by_name(pid, "report", namespace="drafts")
        moved = self.runtime.memory.get_object_by_name(pid, "report.v2", namespace="final")
        self.assertEqual(moved.oid, handle.oid)

    def test_namespace_tools_create_objects_and_list_visible_entries(self) -> None:
        pid = self.runtime.process.spawn(image="base-agent:v0", goal="namespace tools")

        created_ns = self.runtime.tools.call(pid, "create_memory_namespace", {"namespace": "toolspace"})
        created_obj = self.runtime.tools.call(
            pid,
            "create_memory_object",
            {"namespace": "toolspace", "name": "note", "type": "summary", "payload": {"ok": True}},
        )
        listed = self.runtime.tools.call(pid, "list_memory_namespace", {"namespace": "toolspace"})
        listed_default = self.runtime.tools.call(pid, "list_memory_namespace", {})

        self.assertTrue(created_ns.ok, created_ns.error)
        self.assertTrue(created_obj.ok, created_obj.error)
        self.assertEqual(created_obj.payload["namespace"], "toolspace")
        self.assertTrue(listed.ok, listed.error)
        self.assertEqual(listed.payload["objects"][0]["name"], "note")
        self.assertTrue(listed_default.ok, listed_default.error)
        self.assertEqual(listed_default.payload["namespace"], self.runtime.memory.resolve_namespace(pid))

    def test_name_lookup_does_not_bypass_object_capability(self) -> None:
        owner = self.runtime.process.spawn(image="base-agent:v0", goal="owner")
        other = self.runtime.process.spawn(image="base-agent:v0", goal="other")
        handle = self.runtime.memory.create_object(
            pid=owner,
            object_type=ObjectType.EVIDENCE,
            payload={"secret": "owner-only"},
            name="private.evidence",
        )

        with self.assertRaises(NotFound):
            self.runtime.memory.get_object_by_name(other, "private.evidence")

        owner_namespace = self.runtime.memory.resolve_namespace(owner)
        self.runtime.capability.grant(
            subject=other,
            resource=f"object:{handle.oid}",
            rights=[CapabilityRight.READ],
            issued_by="test",
        )
        with self.assertRaises(CapabilityDenied):
            self.runtime.memory.get_object_by_name(other, "private.evidence", namespace=owner_namespace)

        self.runtime.capability.grant(
            subject=other,
            resource=f"object_namespace:{owner_namespace}",
            rights=["read"],
            issued_by="test",
        )
        obj = self.runtime.memory.get_object_by_name(other, "private.evidence", namespace=owner_namespace)
        self.assertEqual(obj.payload, {"secret": "owner-only"})
        with self.assertRaises(CapabilityDenied):
            self.runtime.memory.handle_for_name(other, "private.evidence", rights=["write"], namespace=owner_namespace)

    def test_one_time_object_name_lookup_does_not_become_persistent_handle(self) -> None:
        owner = self.runtime.process.spawn(image="base-agent:v0", goal="owner one-shot")
        by_name_reader = self.runtime.process.spawn(image="base-agent:v0", goal="reader by name")
        handle_reader = self.runtime.process.spawn(image="base-agent:v0", goal="reader handle")
        handle = self.runtime.memory.create_object(
            pid=owner,
            object_type=ObjectType.EVIDENCE,
            payload={"secret": "read once"},
            name="one.shot",
        )
        owner_namespace = self.runtime.memory.resolve_namespace(owner)
        for pid in [by_name_reader, handle_reader]:
            self.runtime.capability.grant(
                subject=pid,
                resource=f"object_namespace:{owner_namespace}",
                rights=["read"],
                issued_by="test",
            )

        self.runtime.capability.grant_once(
            by_name_reader,
            f"object:{handle.oid}",
            [CapabilityRight.READ],
            issued_by="test",
        )
        obj = self.runtime.memory.get_object_by_name(by_name_reader, "one.shot", namespace=owner_namespace)
        self.assertEqual(obj.payload, {"secret": "read once"})
        with self.assertRaises(CapabilityDenied):
            self.runtime.memory.get_object_by_name(by_name_reader, "one.shot", namespace=owner_namespace)

        source_cap = self.runtime.capability.grant_once(
            handle_reader,
            f"object:{handle.oid}",
            [CapabilityRight.READ],
            issued_by="test",
        )
        one_shot_handle = self.runtime.memory.handle_for_name(handle_reader, "one.shot", namespace=owner_namespace)
        self.assertFalse(self.runtime.store.get_capability(source_cap.cap_id).active)
        self.assertEqual(self.runtime.memory.get_object(handle_reader, one_shot_handle).payload, {"secret": "read once"})
        with self.assertRaises(CapabilityDenied):
            self.runtime.memory.get_object(handle_reader, one_shot_handle)

    def test_query_by_name_only_returns_accessible_objects(self) -> None:
        owner = self.runtime.process.spawn(image="base-agent:v0", goal="owner query")
        other = self.runtime.process.spawn(image="base-agent:v0", goal="other query")
        handle = self.runtime.memory.create_object(
            pid=owner,
            object_type=ObjectType.CLAIM,
            payload={"claim": "name lookup is capability checked"},
            name="claim.capability",
        )

        self.assertEqual(self.runtime.memory.query_objects(other, ObjectQuery(name="claim.capability")), [])

        self.runtime.capability.grant(
            subject=other,
            resource=f"object:{handle.oid}",
            rights=[CapabilityRight.READ],
            issued_by="test",
        )
        owner_namespace = self.runtime.memory.resolve_namespace(owner)
        with self.assertRaises(CapabilityDenied):
            self.runtime.memory.query_objects(other, ObjectQuery(name="claim.capability", namespace=owner_namespace))

        self.runtime.capability.grant(
            subject=other,
            resource=f"object_namespace:{owner_namespace}",
            rights=["read"],
            issued_by="test",
        )
        results = self.runtime.memory.query_objects(other, ObjectQuery(name="claim.capability", namespace=owner_namespace))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].oid, handle.oid)

    def test_mutable_object_can_be_renamed_with_unique_name(self) -> None:
        pid = self.runtime.process.spawn(image="base-agent:v0", goal="rename")
        handle = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.ARTIFACT,
            payload={"value": "draft"},
            name="artifact.old",
            immutable=False,
        )
        self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.ARTIFACT,
            payload={"value": "other"},
            name="artifact.other",
        )

        self.runtime.memory.update_object(pid, handle, ObjectPatch(name="artifact.new"))

        with self.assertRaises(NotFound):
            self.runtime.memory.get_object_by_name(pid, "artifact.old")
        self.assertEqual(self.runtime.memory.get_object_by_name(pid, "artifact.new").oid, handle.oid)
        with self.assertRaises(ValidationError):
            self.runtime.memory.update_object(pid, handle, ObjectPatch(name="artifact.other"))

    def test_object_payload_is_not_written_to_sqlite(self) -> None:
        self.runtime.close()
        secret = "SECRET_MEMORY_PAYLOAD_SHOULD_NOT_BE_IN_SQL"
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = f"{temp_dir}/runtime.sqlite"
            runtime = Runtime.open(db_path)
            try:
                pid = runtime.process.spawn(image="base-agent:v0", goal="sqlite payload boundary")
                handle = runtime.memory.create_object(
                    pid=pid,
                    object_type=ObjectType.ARTIFACT,
                    payload={"secret": secret},
                    name="volatile.secret",
                )
                self.assertEqual(runtime.memory.get_object(pid, handle).payload, {"secret": secret})
            finally:
                runtime.close()

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute("SELECT payload_json FROM objects").fetchall()
            finally:
                conn.close()
            serialized = json.dumps(rows)

        self.runtime = Runtime.open("local")
        self.assertNotIn(secret, serialized)
        self.assertIn("runtime_memory", serialized)

    def test_stale_persistent_process_name_does_not_block_new_process_namespace(self) -> None:
        self.runtime.close()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = f"{temp_dir}/runtime.sqlite"
            runtime = Runtime.open(db_path)
            try:
                pid = runtime.process.spawn(image="base-agent:v0", goal="reserve name")
                runtime.memory.create_object(
                    pid=pid,
                    object_type=ObjectType.ARTIFACT,
                    payload={"runtime_only": True},
                    name="reserved.name",
                )
            finally:
                runtime.close()

            reopened = Runtime.open(db_path)
            try:
                pid = reopened.process.spawn(image="base-agent:v0", goal="duplicate stale name")
                handle = reopened.memory.create_object(
                    pid=pid,
                    object_type=ObjectType.ARTIFACT,
                    payload={"new": True},
                    name="reserved.name",
                )
                obj = reopened.memory.get_object(pid, handle)
                self.assertEqual(obj.namespace, reopened.memory.resolve_namespace(pid))
            finally:
                reopened.close()

        self.runtime = Runtime.open("local")

    def test_legacy_name_only_schema_does_not_block_process_namespace(self) -> None:
        self.runtime.close()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = f"{temp_dir}/legacy.sqlite"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE objects (
                      oid TEXT PRIMARY KEY,
                      name TEXT NOT NULL UNIQUE,
                      type TEXT NOT NULL,
                      schema_version TEXT NOT NULL,
                      payload_json TEXT NOT NULL,
                      metadata_json TEXT NOT NULL,
                      provenance_json TEXT NOT NULL,
                      version INTEGER NOT NULL,
                      immutable INTEGER NOT NULL,
                      created_by TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO objects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "obj_legacy",
                        "same.local",
                        "artifact",
                        "1",
                        "{}",
                        "{}",
                        "{}",
                        1,
                        1,
                        "legacy",
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            runtime = Runtime.open(db_path)
            try:
                pid = runtime.process.spawn(image="base-agent:v0", goal="legacy migration")
                runtime.memory.create_namespace(pid, "legacy")
                handle = runtime.memory.create_object(
                    pid=pid,
                    object_type=ObjectType.ARTIFACT,
                    payload={"namespaced": True},
                    name="same.local",
                    namespace="legacy",
                )

                self.assertEqual(runtime.memory.get_object(pid, handle).namespace, "legacy")
                process_handle = runtime.memory.create_object(
                    pid=pid,
                    object_type=ObjectType.ARTIFACT,
                    payload={"process": True},
                    name="same.local",
                )
                self.assertEqual(runtime.memory.get_object(pid, process_handle).namespace, runtime.memory.resolve_namespace(pid))
            finally:
                runtime.close()

        self.runtime = Runtime.open("local")

    def test_process_exit_releases_owned_memory_except_result_object(self) -> None:
        pid = self.runtime.process.spawn(image="base-agent:v0", goal="release memory")
        scratch = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.OBSERVATION,
            payload={"temporary": True},
            name="scratch.memory",
        )
        result = self.runtime.memory.create_object(
            pid=pid,
            object_type=ObjectType.SUMMARY,
            payload={"kept": True},
            name="result.memory",
        )

        self.runtime.process.exit(pid, result=result)

        self.assertIsNone(self.runtime.store.get_object(scratch.oid))
        self.assertIsNotNone(self.runtime.store.get_object(result.oid))
        self.assertEqual(self.runtime.store.get_object(result.oid).payload, {"kept": True})
        with self.assertRaises(NotFound):
            self.runtime.memory.get_object_by_name(pid, "scratch.memory")


if __name__ == "__main__":
    unittest.main()

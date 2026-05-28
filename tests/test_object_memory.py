from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest

from agent_libos import Runtime
from agent_libos.exceptions import CapabilityDenied, NotFound, ValidationError
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

    def test_name_lookup_does_not_bypass_object_capability(self) -> None:
        owner = self.runtime.process.spawn(image="base-agent:v0", goal="owner")
        other = self.runtime.process.spawn(image="base-agent:v0", goal="other")
        handle = self.runtime.memory.create_object(
            pid=owner,
            object_type=ObjectType.EVIDENCE,
            payload={"secret": "owner-only"},
            name="private.evidence",
        )

        with self.assertRaises(CapabilityDenied):
            self.runtime.memory.get_object_by_name(other, "private.evidence")

        self.runtime.capability.grant(
            subject=other,
            resource=f"object:{handle.oid}",
            rights=[CapabilityRight.READ],
            issued_by="test",
        )
        obj = self.runtime.memory.get_object_by_name(other, "private.evidence")

        self.assertEqual(obj.payload, {"secret": "owner-only"})
        with self.assertRaises(CapabilityDenied):
            self.runtime.memory.handle_for_name(other, "private.evidence", rights=["write"])

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
        results = self.runtime.memory.query_objects(other, ObjectQuery(name="claim.capability"))

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

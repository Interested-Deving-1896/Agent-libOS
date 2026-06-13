from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agent_libos import Runtime
from agent_libos.models import CapabilityRight, ObjectMetadata, ObjectType
from agent_libos.models.exceptions import CapabilityDenied, ValidationError


class ImageCommitTests(unittest.TestCase):
    def test_commit_requires_checkpoint_read_and_image_write(self) -> None:
        with _runtime() as runtime:
            pid = runtime.process.spawn(image="base-agent:v0", goal="commit source")
            checkpoint_id = runtime.checkpoint.create(pid, "commit point", actor=pid)

            with self.assertRaises(CapabilityDenied):
                runtime.image_registry.commit_from_checkpoint(
                    actor=pid,
                    checkpoint_id=checkpoint_id,
                    image_id="committed-no-write:v0",
                    name="committed-no-write",
                )

            runtime.image_registry.grant_register(pid, "committed-no-read:v0", issued_by="test")
            other = runtime.process.spawn(image="base-agent:v0", goal="other")
            with self.assertRaises(CapabilityDenied):
                runtime.image_registry.commit_from_checkpoint(
                    actor=other,
                    checkpoint_id=checkpoint_id,
                    image_id="committed-no-read:v0",
                    name="committed-no-read",
                )

    def test_committed_image_spawns_baked_memory_without_external_authority(self) -> None:
        with _runtime() as runtime:
            pid = runtime.process.spawn(image="base-agent:v0", goal="learn state")
            runtime.memory.create_object(
                pid=pid,
                object_type=ObjectType.ARTIFACT,
                payload={"learned": "state"},
                metadata=ObjectMetadata(title="Baked state"),
                name="baked-state",
                immutable=True,
            )
            runtime.filesystem.grant_path(pid, "README.md", [CapabilityRight.READ], issued_by="test")
            runtime.capability.grant(pid, "custom_provider:remote-state", [CapabilityRight.READ], issued_by="test")
            checkpoint_id = runtime.checkpoint.create(pid, "state ready", actor=pid)
            runtime.image_registry.grant_register(pid, "stateful-agent:v0", issued_by="test")

            result = runtime.image_registry.commit_from_checkpoint(
                actor=pid,
                checkpoint_id=checkpoint_id,
                image_id="stateful-agent:v0",
                name="stateful-agent",
            )

            self.assertEqual(result.image.boot["kind"], "checkpoint_commit")
            self.assertTrue(result.image.required_capabilities)
            required_resources = {item["resource"] for item in result.image.required_capabilities}
            self.assertIn("filesystem:workspace:README.md", required_resources)
            self.assertIn("custom_provider:remote-state", required_resources)

            child = runtime.process.spawn(image="stateful-agent:v0", goal="use baked state")
            baked = runtime.memory.get_object_by_name(child, "baked-state")
            self.assertEqual(baked.payload, {"learned": "state"})
            self.assertFalse(runtime.capability.check(child, "filesystem:workspace:README.md", CapabilityRight.READ))
            self.assertFalse(runtime.capability.check(child, "custom_provider:remote-state", CapabilityRight.READ))
            self.assertIn("image.required_capabilities_declared_only", [record.action for record in runtime.audit.trace()])

    def test_exec_into_committed_image_restores_baked_memory_without_granting_required_caps(self) -> None:
        with _runtime() as runtime:
            source = runtime.process.spawn(image="base-agent:v0", goal="source")
            runtime.memory.create_object(
                pid=source,
                object_type=ObjectType.ARTIFACT,
                payload={"role": "committed"},
                metadata=ObjectMetadata(title="Role"),
                name="role",
                immutable=True,
            )
            runtime.capability.grant(source, "shell:python", [CapabilityRight.EXECUTE], issued_by="test")
            checkpoint_id = runtime.checkpoint.create(source, "before commit", actor=source)
            runtime.image_registry.grant_register(source, "exec-state:v0", issued_by="test")
            runtime.image_registry.commit_from_checkpoint(
                actor=source,
                checkpoint_id=checkpoint_id,
                image_id="exec-state:v0",
                name="exec-state",
            )

            target = runtime.process.spawn(image="base-agent:v0", goal="target")
            runtime.exec_process(target, "exec-state:v0", goal="new goal", preserve_capabilities=False)

            self.assertEqual(runtime.memory.get_object_by_name(target, "role").payload, {"role": "committed"})
            self.assertFalse(runtime.capability.check(target, "shell:python", CapabilityRight.EXECUTE))

    def test_duplicate_commit_requires_replace(self) -> None:
        with _runtime() as runtime:
            pid = runtime.process.spawn(image="base-agent:v0", goal="source")
            checkpoint_id = runtime.checkpoint.create(pid, "commit", actor=pid)
            runtime.image_registry.grant_register(pid, "dupe:v0", issued_by="test")
            runtime.image_registry.commit_from_checkpoint(
                actor=pid,
                checkpoint_id=checkpoint_id,
                image_id="dupe:v0",
                name="dupe",
            )
            with self.assertRaises(ValidationError):
                runtime.image_registry.commit_from_checkpoint(
                    actor=pid,
                    checkpoint_id=checkpoint_id,
                    image_id="dupe:v0",
                    name="dupe",
                )

    def test_cli_images_commit_list_and_inspect(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            db_path = str(Path(temp_dir) / "runtime.sqlite")
            spawned = _run_cli_json(["--db", db_path, "spawn", "--goal", "source"])
            created = _run_cli_json(["--db", db_path, "checkpoint", "create", spawned["pid"], "commit"])
            committed = _run_cli_json(
                [
                    "--db",
                    db_path,
                    "images",
                    "commit",
                    created["checkpoint_id"],
                    "cli-committed:v0",
                    "--name",
                    "cli-committed",
                ]
            )
            listed = _run_cli_json(["--db", db_path, "images", "list"])
            inspected = _run_cli_json(["--db", db_path, "images", "inspect", "cli-committed:v0"])

            self.assertEqual(committed["boot"]["kind"], "checkpoint_commit")
            self.assertIn("cli-committed:v0", {item["image_id"] for item in listed})
            self.assertEqual(inspected["image"]["boot"]["kind"], "checkpoint_commit")


def _run_cli_json(args: list[str]) -> object:
    result = subprocess.run(
        [sys.executable, "-m", "agent_libos.api.cli", *args],
        cwd=Path.cwd(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(result.stdout)


@contextmanager
def _runtime() -> Iterator[Runtime]:
    runtime = Runtime.open(":memory:")
    try:
        yield runtime
    finally:
        runtime.shutdown(actor="test", reason="test complete")


if __name__ == "__main__":
    unittest.main()

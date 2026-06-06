from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_libos import Runtime
from agent_libos.api.cli import main as cli_main
from agent_libos.models import CapabilityEffect, CapabilityRight, CapabilitySpec
from agent_libos.models.exceptions import CapabilityDenied, ValidationError
from agent_libos.runtime.syscalls import LibOSSyscallSession


class CapabilityV2ManagerTests(unittest.TestCase):
    def test_typed_resource_matching_rejects_prefix_collision(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="resource matching")
            runtime.capability.grant(pid, "filesystem:workspace:src/*", [CapabilityRight.READ], issued_by="test")

            self.assertTrue(runtime.capability.check(pid, "filesystem:workspace:src/main.py", CapabilityRight.READ))
            self.assertTrue(runtime.capability.check(pid, "filesystem:workspace:src", CapabilityRight.READ))
            self.assertFalse(runtime.capability.check(pid, "filesystem:workspace:src2/main.py", CapabilityRight.READ))
            with self.assertRaises(CapabilityDenied):
                runtime.capability.parse_resource_pattern("filesystem:workspace:src*")
            with self.assertRaises(CapabilityDenied):
                runtime.capability.grant(pid, "*", [CapabilityRight.READ], issued_by="test")
            with self.assertRaises(ValidationError):
                runtime.capability.grant(pid, "filesystem:workspace:src/main.py", ["*"], issued_by="test")
        finally:
            runtime.close()

    def test_deny_dominates_matching_allow(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="deny dominates")
            runtime.capability.grant(pid, "filesystem:workspace:*", [CapabilityRight.READ], issued_by="test")
            runtime.capability.issue_trusted(
                pid,
                "filesystem:workspace:secret.txt",
                [CapabilityRight.READ],
                issued_by="test",
                effect=CapabilityEffect.DENY,
            )

            decision = runtime.capability.authorize(pid, "filesystem:workspace:secret.txt", CapabilityRight.READ)

            self.assertFalse(decision.allowed)
            self.assertEqual(decision.effect, CapabilityEffect.DENY)
            self.assertTrue(decision.matched_capability_ids)
        finally:
            runtime.close()

    def test_one_shot_capability_is_consumed_after_successful_use(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="one shot")
            runtime.capability.grant_once(pid, "filesystem:workspace:once.txt", [CapabilityRight.WRITE], issued_by="test")

            self.assertEqual(
                runtime.capability.permission_policy(pid, "filesystem:workspace:once.txt", CapabilityRight.WRITE),
                runtime.capability.ALLOW_ONCE,
            )
            runtime.capability.consume_allow_once(
                pid,
                "filesystem:workspace:once.txt",
                CapabilityRight.WRITE,
                used_by=pid,
            )

            self.assertFalse(runtime.capability.check(pid, "filesystem:workspace:once.txt", CapabilityRight.WRITE))
        finally:
            runtime.close()

    def test_issue_requires_trusted_actor_or_grant_authority(self) -> None:
        runtime = Runtime.open("local")
        try:
            issuer = runtime.process.spawn(image="base-agent:v0", goal="issuer")
            subject = runtime.process.spawn(image="base-agent:v0", goal="subject")
            spec = CapabilitySpec(resource="object:alpha", rights={CapabilityRight.READ.value})

            with self.assertRaises(CapabilityDenied):
                runtime.capability.issue(issuer, subject, spec)

            runtime.capability.issue_trusted(
                issuer,
                "object:alpha",
                [CapabilityRight.GRANT],
                issued_by="test",
            )
            cap = runtime.capability.issue(issuer, subject, spec)

            self.assertTrue(runtime.capability.check(subject, "object:alpha", CapabilityRight.READ))
            self.assertEqual(cap.issuer_cap_id, runtime.capability.capabilities_for(issuer)[-1].cap_id)
        finally:
            runtime.close()

    def test_actor_names_cannot_gain_trust_by_prefix(self) -> None:
        runtime = Runtime.open("local")
        try:
            subject = runtime.process.spawn(image="base-agent:v0", goal="subject")

            for actor in ["memoryevil", "image:evil", "jit.evil", "process.fork:evil"]:
                with self.assertRaises(CapabilityDenied):
                    runtime.capability.issue(
                        actor,
                        subject,
                        CapabilitySpec(resource="object:prefix-collision", rights={CapabilityRight.READ.value}),
                    )
        finally:
            runtime.close()

    def test_delegate_can_only_attenuate_delegable_parent_capability(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            child = runtime.spawn_child_process(parent, "child")
            runtime.capability.grant(
                parent,
                "filesystem:workspace:src/*",
                [CapabilityRight.READ, CapabilityRight.WRITE],
                issued_by="test",
                delegable=True,
            )

            delegated = runtime.capability.delegate(
                parent,
                child,
                CapabilitySpec(
                    resource="filesystem:workspace:src/main.py",
                    rights={CapabilityRight.READ.value},
                ),
            )

            self.assertEqual(delegated.parent_cap_id, runtime.capability.capabilities_for(parent)[-1].cap_id)
            self.assertTrue(runtime.capability.check(child, "filesystem:workspace:src/main.py", CapabilityRight.READ))
            self.assertFalse(runtime.capability.check(child, "filesystem:workspace:src/main.py", CapabilityRight.WRITE))
            with self.assertRaises(CapabilityDenied):
                runtime.capability.delegate(
                    parent,
                    child,
                    CapabilitySpec(
                        resource="filesystem:workspace:other.py",
                        rights={CapabilityRight.READ.value},
                    ),
                )
        finally:
            runtime.close()

    def test_delegate_cannot_drop_parent_constraints(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            child = runtime.spawn_child_process(parent, "child")
            policy_cap = runtime.capability.grant(
                parent,
                "shell:*",
                [CapabilityRight.EXECUTE],
                issued_by="test",
                constraints={runtime.config.shell.policy_capability_key: runtime.config.shell.always_allow_level},
                delegable=True,
            )

            with self.assertRaises(CapabilityDenied):
                runtime.capability.delegate(
                    parent,
                    child,
                    CapabilitySpec(resource="shell:git", rights={CapabilityRight.EXECUTE.value}),
                )

            delegated = runtime.capability.delegate(
                parent,
                child,
                CapabilitySpec(
                    resource="shell:*",
                    rights={CapabilityRight.EXECUTE.value},
                    constraints=dict(policy_cap.constraints),
                ),
            )

            self.assertEqual(delegated.constraints, policy_cap.constraints)
        finally:
            runtime.close()

    def test_revoke_requires_holder_issuer_or_revoke_authority(self) -> None:
        runtime = Runtime.open("local")
        try:
            owner = runtime.process.spawn(image="base-agent:v0", goal="owner")
            stranger = runtime.process.spawn(image="base-agent:v0", goal="stranger")
            cap = runtime.capability.grant(owner, "object:revocable", [CapabilityRight.READ], issued_by="test")

            with self.assertRaises(CapabilityDenied):
                runtime.capability.revoke(cap.cap_id, revoked_by=stranger)

            runtime.capability.revoke(cap.cap_id, revoked_by=owner, reason="holder abandoned")
            self.assertFalse(runtime.capability.check(owner, "object:revocable", CapabilityRight.READ))
        finally:
            runtime.close()

    def test_holder_cannot_self_revoke_restrictive_capability(self) -> None:
        runtime = Runtime.open("local")
        try:
            owner = runtime.process.spawn(image="base-agent:v0", goal="owner")
            deny = runtime.capability.issue_trusted(
                owner,
                "filesystem:workspace:secret.txt",
                [CapabilityRight.READ],
                issued_by="test",
                effect=CapabilityEffect.DENY,
            )

            with self.assertRaises(CapabilityDenied):
                runtime.capability.revoke(deny.cap_id, revoked_by=owner)

            self.assertFalse(runtime.capability.check(owner, "filesystem:workspace:secret.txt", CapabilityRight.READ))
            runtime.capability.revoke(deny.cap_id, revoked_by="test")
            self.assertEqual(runtime.capability.inspect(deny.cap_id)["status"], "revoked")
        finally:
            runtime.close()


class CapabilityV2RuntimeInterfaceTests(unittest.TestCase):
    def test_default_images_expose_only_low_risk_capability_tools(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="tool table")
            process = runtime.process.get(pid)

            self.assertIn("list_capabilities", process.tool_table)
            self.assertIn("inspect_capability", process.tool_table)
            self.assertNotIn("delegate_capability", process.tool_table)
            self.assertNotIn("revoke_capability", process.tool_table)

            listed = runtime.tools.call(pid, "list_capabilities", {})
            self.assertTrue(listed.ok, listed.error)
            self.assertTrue(listed.payload["capabilities"])
        finally:
            runtime.close()

    def test_capability_syscalls_do_not_bypass_authority(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            child = runtime.spawn_child_process(parent, "child")
            other = runtime.process.spawn(image="base-agent:v0", goal="other")
            runtime.capability.grant(
                parent,
                "object:shared",
                [CapabilityRight.READ],
                issued_by="test",
                delegable=True,
            )
            parent_session = LibOSSyscallSession(runtime, parent)
            other_session = LibOSSyscallSession(runtime, other)

            listed = self._run(parent_session.handle("capability.list", {}))
            delegated = self._run(
                parent_session.handle(
                    "capability.delegate",
                    {
                        "child_pid": child,
                        "resource": "object:shared",
                        "rights": [CapabilityRight.READ.value],
                    },
                )
            )

            self.assertTrue(listed["capabilities"])
            self.assertTrue(runtime.capability.check(child, "object:shared", CapabilityRight.READ))
            self.assertEqual(delegated["capability"]["subject"], child)
            with self.assertRaises(CapabilityDenied):
                self._run(other_session.handle("capability.inspect", {"capability_id": delegated["capability"]["cap_id"]}))
            with self.assertRaises(CapabilityDenied):
                self._run(
                    parent_session.handle(
                        "capability.delegate",
                        {
                            "child_pid": other,
                            "resource": "object:shared",
                            "rights": [CapabilityRight.READ.value],
                        },
                    )
                )

            deny = runtime.capability.issue_trusted(
                parent,
                "object:blocked",
                [CapabilityRight.READ],
                issued_by="test",
                effect=CapabilityEffect.DENY,
            )
            with self.assertRaises(CapabilityDenied):
                self._run(parent_session.handle("capability.revoke", {"capability_id": deny.cap_id}))
        finally:
            runtime.close()

    def test_spawn_child_invalid_capability_inheritance_is_preflighted(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            runtime.capability.grant(
                parent,
                "shell:*",
                [CapabilityRight.EXECUTE],
                issued_by="test",
                constraints={runtime.config.shell.policy_capability_key: runtime.config.shell.always_allow_level},
                delegable=True,
            )
            before = len(runtime.process.list())

            with self.assertRaises(CapabilityDenied):
                runtime.spawn_child_process(
                    parent,
                    "should fail",
                    inherit_capabilities=[{"resource": "shell:git", "rights": [CapabilityRight.EXECUTE.value]}],
                )

            self.assertEqual(len(runtime.process.list()), before)
        finally:
            runtime.close()

    def test_capabilities_cli_outputs_stable_json_and_enforces_actor_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "runtime.sqlite")
            parent = _run_cli_json(["--db", db_path, "spawn", "--goal", "parent"])
            granted = _run_cli_json(
                [
                    "--db",
                    db_path,
                    "capabilities",
                    "grant",
                    parent["pid"],
                    "object:cli",
                    "--rights",
                    "read",
                    "--delegable",
                ]
            )
            listed = _run_cli_json(["--db", db_path, "capabilities", "list", "--subject", parent["pid"]])
            explained = _run_cli_json(
                [
                    "--db",
                    db_path,
                    "capabilities",
                    "explain",
                    parent["pid"],
                    "object:cli",
                    "read",
                ]
            )

            self.assertEqual(granted["subject"], parent["pid"])
            self.assertIn(granted["cap_id"], {capability["cap_id"] for capability in listed})
            self.assertTrue(explained["allowed"])
            with self.assertRaises(CapabilityDenied):
                _run_cli_json(
                    [
                        "--db",
                        db_path,
                        "capabilities",
                        "--actor-pid",
                        parent["pid"],
                        "grant",
                        "other",
                        "object:denied",
                        "--rights",
                        "read",
                    ]
                )

    def _run(self, awaitable):
        import asyncio

        return asyncio.run(awaitable)


def _run_cli_json(argv: list[str]):
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        cli_main(argv)
    return json.loads(stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

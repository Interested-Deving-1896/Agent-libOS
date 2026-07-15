from __future__ import annotations

from pathlib import Path

import pytest

from agent_libos import Runtime
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models import CapabilityRight
from agent_libos.models.exceptions import CapabilityDenied, ValidationError


def test_image_requirements_are_declared_but_not_granted_by_default() -> None:
    runtime = Runtime.open("local")
    try:
        pid = runtime.process.spawn(image="base-agent:v0", goal="manifest required")
        manifest = runtime.authority_manifests.get_for_process(pid)

        assert manifest is not None
        assert manifest.metadata["launch_authority_mode"] == "manifest_required"
        assert manifest.authorized_capabilities == []
        assert manifest.required_capabilities
        assert not runtime.capability.check(pid, DEFAULT_CONFIG.runtime.default_human_resource, "write")
        assert runtime.authority_manifests.summary_for_process(pid)["missing_required_capabilities"]
    finally:
        runtime.close()


def test_host_manifest_is_hashed_persisted_and_compiles_only_declared_authority(tmp_path: Path) -> None:
    database = tmp_path / "manifest.sqlite"
    runtime = Runtime.open(database)
    try:
        pid = runtime.process.spawn(
            image="base-agent:v0",
            goal="explicit manifest",
            authority_manifest={
                "authorized_capabilities": [
                    {
                        "resource": DEFAULT_CONFIG.runtime.default_human_resource,
                        "rights": [CapabilityRight.WRITE.value],
                    }
                ],
                "permitted_effects": ["human.*"],
                "metadata": {"contract": "test"},
            },
        )
        manifest = runtime.authority_manifests.get_for_process(pid)
        assert manifest is not None and len(manifest.manifest_hash) == 64
        assert runtime.capability.check(pid, DEFAULT_CONFIG.runtime.default_human_resource, "write")
        assert not runtime.capability.check(pid, "filesystem:workspace:*", "read")
        manifest_id = manifest.manifest_id
    finally:
        runtime.close()

    reopened = Runtime.open(database)
    try:
        restored = reopened.authority_manifests.get(manifest_id)
        assert restored.pid == pid
        assert reopened.authority_manifests.summary_for_process(pid)["manifest_hash"] == restored.manifest_hash
    finally:
        reopened.close()


def test_model_permission_request_outside_manifest_is_denied_before_human_request() -> None:
    runtime = Runtime.open("local")
    try:
        pid = runtime.process.spawn(
            image="base-agent:v0",
            goal="bounded request",
            authority_manifest={
                "authorized_capabilities": [
                    {
                        "resource": DEFAULT_CONFIG.runtime.default_human_resource,
                        "rights": [CapabilityRight.WRITE.value],
                    }
                ]
            },
        )

        with pytest.raises(CapabilityDenied, match="exceeds task authority manifest"):
            runtime.human.request_permission(
                pid,
                DEFAULT_CONFIG.runtime.default_human,
                "filesystem:workspace:outside.txt",
                [CapabilityRight.WRITE.value],
                "outside launch contract",
            )

        assert runtime.human.list(pid=pid) == []
    finally:
        runtime.close()


def test_requestable_manifest_authority_allows_prompt_without_pregranting_capability() -> None:
    runtime = Runtime.open("local")
    try:
        resource = "filesystem:workspace:report.txt"
        pid = runtime.process.spawn(
            image="base-agent:v0",
            goal="requestable authority",
            authority_manifest={
                "authorized_capabilities": [
                    {
                        "resource": DEFAULT_CONFIG.runtime.default_human_resource,
                        "rights": [CapabilityRight.WRITE.value],
                    }
                ],
                "approval_policy": {
                    "requestable_capabilities": [
                        {"resource": resource, "rights": [CapabilityRight.WRITE.value]},
                    ]
                },
            },
        )
        assert not runtime.capability.check(pid, resource, CapabilityRight.WRITE)

        request_id = runtime.human.request_permission(
            pid,
            DEFAULT_CONFIG.runtime.default_human,
            resource,
            [CapabilityRight.WRITE.value],
            "write the report",
        )

        assert runtime.human.get(request_id).status.value == "pending"
        assert not runtime.capability.check(pid, resource, CapabilityRight.WRITE)
    finally:
        runtime.close()


def test_implicit_manifest_denies_model_requests_but_preserves_host_transition_authority() -> None:
    runtime = Runtime.open("local")
    try:
        parent = runtime.process.spawn(image="base-agent:v0", goal="implicit host authority")
        resource = "filesystem:workspace:reports/host-granted.txt"

        with pytest.raises(CapabilityDenied, match="exceeds task authority manifest"):
            runtime.authority_manifests.assert_capability_request(
                parent,
                resource,
                [CapabilityRight.READ.value],
            )

        runtime.capability.grant(
            parent,
            "process:spawn",
            [CapabilityRight.WRITE],
            issued_by="host:test",
        )
        runtime.capability.grant(
            parent,
            resource,
            [CapabilityRight.READ],
            issued_by="host:test",
            delegable=True,
        )
        child = runtime.process.spawn_child(
            parent,
            "derived host authority",
            inherit_capabilities=[
                {"resource": resource, "rights": [CapabilityRight.READ.value]}
            ],
        )

        assert runtime.capability.check(child, resource, CapabilityRight.READ)
    finally:
        runtime.close()


def test_child_manifest_and_transition_api_enforce_parent_intersection() -> None:
    runtime = Runtime.open("local")
    try:
        parent = runtime.process.spawn(
            image="base-agent:v0",
            goal="parent",
            authority_manifest={
                "authorized_capabilities": [
                    {
                        "resource": "filesystem:workspace:reports/*",
                        "rights": [CapabilityRight.READ.value],
                        "delegable": True,
                    }
                ]
            },
        )
        child_spec = {
            "resource": "filesystem:workspace:reports/q1.txt",
            "rights": [CapabilityRight.READ.value],
        }
        child = runtime.process.spawn_child(
            parent,
            "child",
            authority_manifest={"authorized_capabilities": [child_spec]},
        )
        assert runtime.capability.check(child, child_spec["resource"], CapabilityRight.READ)
        child_manifest = runtime.authority_manifests.get_for_process(child)
        assert child_manifest is not None
        assert child_manifest.parent_manifest_id == runtime.authority_manifests.get_for_process(parent).manifest_id

        forked = runtime.process.fork(
            parent,
            "forked child",
            authority_manifest={"authorized_capabilities": [child_spec]},
        )
        assert runtime.capability.check(forked, child_spec["resource"], CapabilityRight.READ)
        forked_manifest = runtime.authority_manifests.get_for_process(forked)
        assert forked_manifest is not None
        assert forked_manifest.authorized_capabilities == child_manifest.authorized_capabilities

        outside = {
            "resource": "filesystem:workspace:secrets/key.txt",
            "rights": [CapabilityRight.READ.value],
        }
        with pytest.raises(CapabilityDenied, match="derived child authority"):
            runtime.process.spawn_child(
                parent,
                "outside",
                authority_manifest={"authorized_capabilities": [outside]},
            )
    finally:
        runtime.close()


def test_child_manifest_cannot_widen_parent_policy_ceilings() -> None:
    runtime = Runtime.open("local")
    try:
        parent_resource = "filesystem:workspace:reports/*"
        child_resource = "filesystem:workspace:reports/q1.txt"
        parent = runtime.process.spawn(
            image="base-agent:v0",
            goal="parent policy ceiling",
            authority_manifest={
                "authorized_capabilities": [
                    {
                        "resource": parent_resource,
                        "rights": [CapabilityRight.READ.value],
                        "delegable": True,
                    }
                ],
                "permitted_effects": ["filesystem.*"],
                "approval_policy": {
                    "mode": "operator",
                    "requestable_capabilities": [
                        {
                            "resource": parent_resource,
                            "rights": [CapabilityRight.READ.value],
                        }
                    ],
                },
                "data_flow_policy": {
                    "schema_version": 1,
                    "allowed_tenants": ["tenant-a"],
                    "allowed_principals": [],
                },
                "expires_at": "2030-01-01T00:00:00Z",
            },
        )
        child_spec = {
            "resource": child_resource,
            "rights": [CapabilityRight.READ.value],
        }
        child = runtime.process.spawn_child(
            parent,
            "inherited policy ceiling",
            capabilities=[child_spec],
            authority_manifest={"authorized_capabilities": [child_spec]},
        )
        child_manifest = runtime.authority_manifests.get_for_process(child)
        assert child_manifest is not None
        assert child_manifest.permitted_effects == ["filesystem.*"]
        assert child_manifest.approval_policy["mode"] == "operator"
        assert child_manifest.data_flow_policy == {
            "schema_version": 1,
            "allowed_tenants": ["tenant-a"],
            "allowed_principals": [],
        }
        assert child_manifest.expires_at == "2030-01-01T00:00:00Z"

        with pytest.raises(CapabilityDenied, match="effect ceiling"):
            runtime.process.spawn_child(
                parent,
                "widen effects",
                capabilities=[child_spec],
                authority_manifest={
                    "authorized_capabilities": [child_spec],
                    "permitted_effects": ["jsonrpc.*"],
                },
            )
        with pytest.raises(CapabilityDenied, match="requestable capability"):
            runtime.process.spawn_child(
                parent,
                "widen requestable authority",
                capabilities=[child_spec],
                authority_manifest={
                    "authorized_capabilities": [child_spec],
                    "approval_policy": {
                        "requestable_capabilities": [
                            {
                                "resource": "filesystem:workspace:secrets/*",
                                "rights": [CapabilityRight.WRITE.value],
                            }
                        ]
                    },
                },
            )
        with pytest.raises(CapabilityDenied, match="expiry"):
            runtime.process.spawn_child(
                parent,
                "widen expiry",
                capabilities=[child_spec],
                authority_manifest={
                    "authorized_capabilities": [child_spec],
                    "expires_at": "2040-01-01T00:00:00Z",
                },
            )
        with pytest.raises(CapabilityDenied, match="data_flow_policy"):
            runtime.process.spawn_child(
                parent,
                "replace data flow policy",
                capabilities=[child_spec],
                authority_manifest={
                    "authorized_capabilities": [child_spec],
                    "data_flow_policy": {
                        "schema_version": 1,
                        "allowed_tenants": ["tenant-b"],
                        "allowed_principals": [],
                    },
                },
            )
        with pytest.raises(ValidationError, match="unsupported fields"):
            runtime.process.spawn_child(
                parent,
                "add data flow escape",
                capabilities=[child_spec],
                authority_manifest={
                    "authorized_capabilities": [child_spec],
                    "data_flow_policy": {
                        "schema_version": 1,
                        "allowed_tenants": ["tenant-a"],
                        "allowed_principals": [],
                        "allow_external": True,
                    },
                },
            )
    finally:
        runtime.close()


def test_manifest_max_delegation_depth_is_compiled_and_cannot_be_broadened() -> None:
    runtime = Runtime.open("local")
    try:
        parent = runtime.process.spawn(
            goal="non-delegable manifest ceiling",
            authority_manifest={
                "authorized_capabilities": [
                    {
                        "resource": "filesystem:workspace:reports/*",
                        "rights": [CapabilityRight.READ.value],
                        "delegable": True,
                        "max_delegation_depth": 0,
                    }
                ]
            },
        )
        capability = next(
            item
            for item in runtime.capability.capabilities_for(parent)
            if item.resource == "filesystem:workspace:reports/*"
        )

        assert capability.max_delegation_depth == 0
        with pytest.raises(CapabilityDenied, match="delegation depth exhausted"):
            runtime.capability.delegate(
                parent,
                "pid_child",
                {
                    "resource": "filesystem:workspace:reports/q1.txt",
                    "rights": [CapabilityRight.READ.value],
                },
            )
        assert not runtime.capability.spec_covers(
            {
                "resource": "filesystem:workspace:reports/*",
                "rights": [CapabilityRight.READ.value],
                "delegable": True,
                "max_delegation_depth": 1,
            },
            {
                "resource": "filesystem:workspace:reports/q1.txt",
                "rights": [CapabilityRight.READ.value],
                "delegable": True,
                "max_delegation_depth": 2,
            },
        )
    finally:
        runtime.close()


def test_checkpoint_fork_preserves_explicit_manifest_effect_ceiling() -> None:
    runtime = Runtime.open("local")
    try:
        parent = runtime.process.spawn(
            goal="checkpoint manifest source",
            authority_manifest={
                "authorized_capabilities": [
                    {
                        "resource": "jsonrpc:demo:update",
                        "rights": [CapabilityRight.WRITE.value],
                    }
                ],
                "permitted_effects": ["filesystem.*"],
            },
        )
        checkpoint_id = runtime.checkpoint.create(parent, "fork manifest", require_capability=False)

        fork = runtime.checkpoint.fork_from_checkpoint(
            parent,
            checkpoint_id,
            require_capability=False,
        )
        fork_pid = fork["fork_root_pid"]
        manifest = runtime.authority_manifests.get_for_process(fork_pid)

        assert manifest is not None
        assert manifest.permitted_effects == ["filesystem.*"]
        assert runtime.capability.check(fork_pid, "jsonrpc:demo:update", CapabilityRight.WRITE)
        with pytest.raises(CapabilityDenied, match="does not permit effect class"):
            runtime.authority_manifests.assert_effect(fork_pid, "jsonrpc.call")
    finally:
        runtime.close()

#!/usr/bin/env python3
"""Configure and inspect LLM Sink trust locally, without a provider request.

Run from the repository root:
    uv run python examples/data_flow/sink_trust_demo.py

The named profile is an identity fixture, not a deployed model. All state lives
in a temporary SQLite database and workspace and is removed when the demo exits.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_libos import Runtime
from agent_libos.config import DEFAULT_CONFIG, LLMProfile
from agent_libos.models import (
    CapabilityRight,
    DataFlowOutcome,
    DataSink,
    ObjectMetadata,
    ObjectType,
    SinkTrustLevel,
    SinkTrustRule,
)
from agent_libos.models.exceptions import CapabilityDenied
from agent_libos.substrate import LocalResourceProviderSubstrate


def demonstrate(runtime: Runtime) -> dict[str, str | int]:
    profile_id = "sink-trust-demo"
    # This reads the already configured profile; it neither resolves a client
    # nor calls the provider. Do not hash a model name or endpoint yourself.
    identity_sha256 = runtime.llms.profile_identity_sha256(profile_id)
    sink = DataSink(f"llm:{profile_id}", identity_sha256)
    admin_pid = runtime.process.spawn(goal="Host Sink registry administration")
    task_pid = runtime.process.spawn(
        goal="Inspect the clearance of a synthetic confidential Object",
        authority_manifest={
            "data_flow_policy": {
                "schema_version": 1,
                "allowed_tenants": ["tenant-a"],
                "allowed_principals": ["analyst-a"],
            }
        },
    )
    source = runtime.memory.create_object(
        task_pid,
        ObjectType.EVIDENCE,
        {"text": "Synthetic example; no real confidential data."},
        metadata=ObjectMetadata(
            sensitivity="confidential", tenant="tenant-a", principal="analyst-a"
        ),
    )
    context = runtime.data_flow.context_from_source_oids(task_pid, [source.oid])
    rule = SinkTrustRule(
        pattern=sink.identity,
        trust_level=SinkTrustLevel.TRUSTED,
        max_sensitivity="confidential",
        tenants=("tenant-a",),
        principals=("analyst-a",),
        identity_sha256=identity_sha256,
    )

    before = runtime.data_flow.classify_egress_snapshot(sink=sink, context=context)
    assert before is DataFlowOutcome.DENY
    try:
        runtime.register_sink_trust(rule, actor=task_pid)
    except CapabilityDenied:
        pass
    else:
        raise AssertionError("Sink registration succeeded without registry admin")
    assert runtime.inspect_sink_trust(sink.identity) is None

    # Only trusted embedding Host code may issue this grant. The actor string
    # alone is not authority, and this helper must never become a model tool.
    runtime.capability.issue_trusted(
        admin_pid,
        runtime.config.data_flow.registry_resource,
        [CapabilityRight.ADMIN],
        issued_by="examples.data_flow.host",
    )
    registered = runtime.register_sink_trust(rule, actor=admin_pid)
    assert runtime.inspect_sink_trust(sink.identity) == registered
    assert registered in runtime.list_sink_trust()

    after = runtime.data_flow.classify_egress_snapshot(sink=sink, context=context)
    assert after is DataFlowOutcome.ALLOW

    # A later Host profile change produces a new identity. The unchanged trust
    # rule must reject it until the Host deliberately reviews and replaces it.
    runtime.llms.register_profile(
        profile_id,
        replace(runtime.llms.profile(profile_id), model="offline-identity-demo-v2"),
    )
    changed_identity = runtime.llms.profile_identity_sha256(profile_id)
    assert changed_identity != identity_sha256
    drift = runtime.data_flow.classify_egress_snapshot(
        sink=DataSink(sink.identity, changed_identity), context=context
    )
    assert drift is DataFlowOutcome.DENY

    # A clearance snapshot creates no approval, dispatch, or effect intent.
    assert runtime.store.list_external_effects(pid=task_pid) == []
    return {
        "sink": sink.identity,
        "identity_sha256": identity_sha256,
        "registry_generation": registered.generation,
        "registration_without_admin": "denied",
        "before_registration": before.value,
        "after_registration": after.value,
        "after_profile_change": drift.value,
        "provider_requests": 0,
    }


def main() -> None:
    config = replace(
        DEFAULT_CONFIG,
        llm=replace(
            DEFAULT_CONFIG.llm,
            profiles={
                **DEFAULT_CONFIG.llm.profiles,
                "sink-trust-demo": LLMProfile(
                    model="offline-identity-demo-v1", api_mode="chat", store=False
                ),
            },
        ),
    )
    with TemporaryDirectory(prefix="agent-libos-sink-trust-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        runtime = Runtime.open(
            root / "runtime.db",
            substrate=LocalResourceProviderSubstrate(workspace),
            config=config,
        )
        try:
            result = demonstrate(runtime)
        finally:
            cleanup = runtime.close()
            if not cleanup.get("ok"):
                raise RuntimeError(f"Runtime cleanup did not complete: {cleanup}")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

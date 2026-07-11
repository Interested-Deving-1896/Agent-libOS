from __future__ import annotations

import pytest

from agent_libos.models import CapabilityRight, ObjectMetadata, ObjectPatch, ObjectType, Provenance
from agent_libos.models.exceptions import CapabilityDenied
from tests.support.runtime import temporary_runtime


def test_derived_object_conservatively_propagates_sensitivity_and_trust() -> None:
    with temporary_runtime() as runtime:
        pid = runtime.process.spawn(goal="labels")
        trusted = runtime.memory.create_object(
            pid,
            ObjectType.EVIDENCE,
            {"source": "internal"},
            metadata=ObjectMetadata(
                sensitivity="confidential",
                trust_level="verified",
                integrity="checked",
                origin="internal-db",
                tenant="tenant-a",
            ),
        )
        untrusted = runtime.memory.create_object(
            pid,
            ObjectType.EVIDENCE,
            {"source": "remote"},
            metadata=ObjectMetadata(
                sensitivity="restricted",
                trust_level="untrusted",
                integrity="untrusted",
                origin="remote-provider",
                tenant="tenant-a",
            ),
        )
        derived = runtime.memory.create_object(
            pid,
            ObjectType.SUMMARY,
            {"summary": "derived"},
            metadata=ObjectMetadata(sensitivity="normal", trust_level="trusted", integrity="verified"),
            provenance=Provenance(
                created_from_action="llm.create_memory_object",
                parent_oids=[trusted.oid, untrusted.oid],
            ),
        )
        obj = runtime.memory.get_object(pid, derived)

        assert obj.metadata.sensitivity == "restricted"
        assert obj.metadata.trust_level == "untrusted"
        assert obj.metadata.integrity == "untrusted"
        assert obj.metadata.origin == "derived"
        assert obj.metadata.tenant == "tenant-a"


def test_context_manifest_and_explain_expose_labels_without_payload() -> None:
    with temporary_runtime() as runtime:
        pid = runtime.process.spawn(goal="explain labels")
        handle = runtime.memory.create_object(
            pid,
            ObjectType.ARTIFACT,
            {"secret": "LABEL_PAYLOAD_MUST_NOT_LEAK"},
            metadata=ObjectMetadata(sensitivity="secret", trust_level="untrusted", origin="remote"),
        )
        view = runtime.memory.create_view(pid, [handle])
        context = runtime.memory.materialize_context(pid, view, charge_resources=False)

        assert context.object_manifest[0]["labels"]["sensitivity"] == "secret"
        assert context.object_manifest[0]["labels"]["trust_level"] == "untrusted"
        assert "LABEL_PAYLOAD_MUST_NOT_LEAK" not in str(context.object_manifest)


def test_label_downgrade_requires_explicit_declassification_capability() -> None:
    with temporary_runtime() as runtime:
        pid = runtime.process.spawn(goal="declassification")
        handle = runtime.memory.create_object(
            pid,
            ObjectType.ARTIFACT,
            {"value": "classified"},
            metadata=ObjectMetadata(sensitivity="secret"),
            immutable=False,
        )
        with pytest.raises(CapabilityDenied):
            runtime.memory.update_object(
                pid,
                handle,
                ObjectPatch(metadata=ObjectMetadata(sensitivity="public")),
            )

        runtime.capability.issue_trusted(
            pid,
            f"declassification:object:{handle.oid}",
            [CapabilityRight.ADMIN],
            issued_by="test",
        )
        runtime.memory.update_object(
            pid,
            handle,
            ObjectPatch(metadata=ObjectMetadata(sensitivity="public")),
        )
        assert runtime.memory.get_object(pid, handle).metadata.sensitivity == "public"


def test_finite_declassification_capability_is_consumed_once() -> None:
    with temporary_runtime() as runtime:
        pid = runtime.process.spawn(goal="finite declassification")
        handle = runtime.memory.create_object(
            pid,
            ObjectType.ARTIFACT,
            {"value": "classified"},
            metadata=ObjectMetadata(sensitivity="secret"),
            immutable=False,
        )
        capability = runtime.capability.issue_trusted(
            pid,
            f"declassification:object:{handle.oid}",
            [CapabilityRight.ADMIN],
            issued_by="test",
            uses_remaining=1,
        )

        runtime.memory.update_object(
            pid,
            handle,
            ObjectPatch(metadata=ObjectMetadata(sensitivity="public")),
        )
        consumed = runtime.store.get_capability(capability.cap_id)
        assert consumed is not None and consumed.uses_remaining == 0

        runtime.memory.update_object(
            pid,
            handle,
            ObjectPatch(metadata=ObjectMetadata(sensitivity="secret")),
        )
        with pytest.raises(CapabilityDenied):
            runtime.memory.update_object(
                pid,
                handle,
                ObjectPatch(metadata=ObjectMetadata(sensitivity="public")),
            )

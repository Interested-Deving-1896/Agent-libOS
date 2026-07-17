from __future__ import annotations

from contextlib import contextmanager

import pytest

from agent_libos.models import Checkpoint
from agent_libos.models.exceptions import ValidationError
from agent_libos.runtime.snapshots import (
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotCodec,
    SnapshotCoordinator,
    SnapshotIdentityMap,
    SnapshotRemapper,
    SnapshotRows,
    SnapshotVersionError,
)


def _row(table: str, **values: object) -> dict[str, object]:
    return {**{column: None for column in SnapshotRows.ROW_COLUMNS[table]}, **values}


def _snapshot() -> dict:
    return {
        "version": SNAPSHOT_SCHEMA_VERSION,
        "checkpoint_id": "ckpt_1",
        "pid": "pid_1",
        "reason": "test",
        "created_at": "2040-01-01T00:00:00Z",
        "created_by": "test",
        "subtree_pids": ["pid_1"],
        "object_oids": ["obj_1"],
        "owned_object_oids": ["obj_1"],
        "referenced_object_oids": ["obj_1"],
        "referenced_object_types": {"obj_1": "TEXT"},
        "namespaces": ["proc:pid_1"],
        "owned_namespaces": ["proc:pid_1"],
        "rows": {
            "processes": [_row("processes", pid="pid_1", goal_oid="obj_1")],
            "object_namespaces": [_row("object_namespaces", namespace="proc:pid_1")],
            "objects": [_row("objects", oid="obj_1", namespace="proc:pid_1")],
            "object_links": [],
            "capabilities": [_row("capabilities", cap_id="cap_1", subject="pid_1")],
            "process_resource_reservations": [],
            "process_messages": [],
            "llm_pending_actions": [],
            "skills": [],
            "tools": [_row("tools", tool_id="tool_1")],
            "tool_candidates": [],
        },
        "object_payloads": {"obj_1": {"text": "hello"}},
        "images": {},
        "image_artifacts": {},
        "jit_sources": {"tool_1": "export default {}"},
        "modules": [{"module_id": "core", "source_sha256": "abc"}],
    }


def test_snapshot_codec_round_trip_is_strict_and_lossless() -> None:
    snapshot = SnapshotCodec.decode_mapping(_snapshot())
    encoded = SnapshotCodec.dumps(snapshot)
    assert SnapshotCodec.loads(encoded) == snapshot
    assert SnapshotCodec.encode_mapping(snapshot) == _snapshot()


def test_checkpoint_model_uses_the_codec_protocol_version() -> None:
    checkpoint = Checkpoint(
        checkpoint_id="ckpt_1",
        pid="pid_1",
        reason="test",
        created_at="2040-01-01T00:00:00Z",
    )

    assert checkpoint.snapshot_version == SnapshotCodec.schema_version


def test_snapshot_codec_rejects_old_versions_and_unknown_tables() -> None:
    old = _snapshot()
    old["version"] = 1
    with pytest.raises(SnapshotVersionError):
        SnapshotCodec.decode_mapping(old)

    unknown = _snapshot()
    unknown["rows"]["arbitrary_table"] = []
    with pytest.raises(ValidationError, match="unsupported row tables"):
        SnapshotCodec.decode_mapping(unknown)

    incomplete = _snapshot()
    incomplete["rows"]["processes"][0].pop("status")
    with pytest.raises(ValidationError, match="not canonical"):
        SnapshotCodec.decode_mapping(incomplete)


def test_snapshot_codec_rejects_missing_tables_and_process_rows() -> None:
    missing_table = _snapshot()
    missing_table["rows"].pop("processes")
    with pytest.raises(ValidationError, match="missing tables"):
        SnapshotCodec.decode_mapping(missing_table)

    missing_process = _snapshot()
    missing_process["rows"]["processes"] = []
    with pytest.raises(ValidationError, match="exactly match subtree_pids"):
        SnapshotCodec.decode_mapping(missing_process)


def test_snapshot_remapper_updates_typed_identity_fields() -> None:
    snapshot = SnapshotCodec.decode_mapping(_snapshot())
    remapped = SnapshotRemapper.remap(
        snapshot,
        SnapshotIdentityMap(
            pids={"pid_1": "pid_2"},
            objects={"obj_1": "obj_2"},
            namespaces={"proc:pid_1": "proc:pid_2"},
            capabilities={"cap_1": "cap_2"},
            tools={"tool_1": "tool_2"},
        ),
    )
    encoded = SnapshotCodec.encode_mapping(remapped)
    assert encoded["pid"] == "pid_2"
    assert encoded["rows"]["processes"][0]["goal_oid"] == "obj_2"
    assert encoded["rows"]["capabilities"][0]["cap_id"] == "cap_2"
    assert encoded["rows"]["capabilities"][0]["subject"] == "pid_2"
    assert encoded["object_payloads"] == {"obj_2": {"text": "hello"}}
    assert encoded["jit_sources"] == {"tool_2": "export default {}"}


def test_snapshot_identity_maps_must_be_one_to_one() -> None:
    with pytest.raises(ValidationError, match="one-to-one"):
        SnapshotIdentityMap(pids={"pid_1": "pid_3", "pid_2": "pid_3"})


class _CoordinatorStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    @contextmanager
    def transaction(self, *, include_object_payloads: bool = False):
        assert include_object_payloads is True
        self.calls.append("transaction.enter")
        try:
            yield self
        finally:
            self.calls.append("transaction.exit")

    @contextmanager
    def locked(self):
        self.calls.append("store.enter")
        try:
            yield
        finally:
            self.calls.append("store.exit")


def test_snapshot_coordinator_compensates_reservation_after_publish_failure() -> None:
    store = _CoordinatorStore()
    coordinator = SnapshotCoordinator(store)

    def fail_publish(snapshot, prepared):
        assert snapshot.header.checkpoint_id == "ckpt_1"
        assert prepared == "prepared"
        store.calls.append("publish")
        raise RuntimeError("injected publish failure")

    with pytest.raises(RuntimeError, match="publish failure"):
        coordinator.atomic_publish(
            _snapshot(),
            reserve=lambda: store.calls.append("reserve") or "reservation",
            prepare=lambda _snapshot: store.calls.append("prepare") or "prepared",
            settle=lambda token: store.calls.append(f"settle:{token}"),
            publish=fail_publish,
            compensate=lambda token: store.calls.append(f"compensate:{token}"),
        )

    assert store.calls == [
        "reserve",
        "prepare",
        "transaction.enter",
        "settle:reservation",
        "publish",
        "transaction.exit",
        "compensate:reservation",
    ]


def test_snapshot_coordinator_exposes_canonical_restore_lock_order() -> None:
    store = _CoordinatorStore()
    coordinator = SnapshotCoordinator(store)

    def scope(name: str):
        @contextmanager
        def selected():
            store.calls.append(f"{name}.enter")
            try:
                yield
            finally:
                store.calls.append(f"{name}.exit")

        return selected

    with coordinator.restore_runtime_scope(scope("runtime")):
        with coordinator.restore_registry_scope(scope("registry")):
            with coordinator.restore_atomic_scope(scope("ownership")):
                store.calls.append("publish")
            store.calls.append("registry.finalize")
        store.calls.append("host.finalize")

    assert store.calls == [
        "runtime.enter",
        "registry.enter",
        "ownership.enter",
        "store.enter",
        "publish",
        "store.exit",
        "ownership.exit",
        "registry.finalize",
        "registry.exit",
        "host.finalize",
        "runtime.exit",
    ]

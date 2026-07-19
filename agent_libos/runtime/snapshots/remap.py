from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping

from agent_libos.models.exceptions import ValidationError
from agent_libos.models.process_state import (
    legacy_status_message,
    process_outcome_from_json,
    process_outcome_to_mapping,
    process_wait_state_from_json,
    process_wait_state_to_mapping,
    remap_process_outcome,
    remap_process_wait_state,
)
from agent_libos.runtime.snapshots.models import ProcessSnapshot, SnapshotHeader, SnapshotRows
from agent_libos.utils.serde import dumps


@dataclass(frozen=True)
class SnapshotIdentityMap:
    pids: Mapping[str, str] = field(default_factory=dict)
    objects: Mapping[str, str] = field(default_factory=dict)
    namespaces: Mapping[str, str] = field(default_factory=dict)
    capabilities: Mapping[str, str] = field(default_factory=dict)
    tools: Mapping[str, str] = field(default_factory=dict)
    candidates: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("pids", "objects", "namespaces", "capabilities", "tools", "candidates"):
            selected = dict(getattr(self, name))
            if len(selected.values()) != len(set(selected.values())):
                raise ValidationError(f"snapshot identity map {name} must be one-to-one")
            if any(not str(source) or not str(target) for source, target in selected.items()):
                raise ValidationError(f"snapshot identity map {name} contains an empty id")


class SnapshotRemapper:
    """Pure remapping helpers shared by fork, exec rollback, and image commit."""

    _FIELD_MAPS = {
        "pid": "pids",
        "parent_pid": "pids",
        "subject": "pids",
        "creator_pid": "pids",
        "recipient_pid": "pids",
        "sender": "pids",
        "sender_pid": "pids",
        "owner_pid": "pids",
        "runner_pid": "pids",
        "child_pid": "pids",
        "created_by": "pids",
        "oid": "objects",
        "src_oid": "objects",
        "dst_oid": "objects",
        "result_oid": "objects",
        "goal_oid": "objects",
        "namespace": "namespaces",
        "parent_namespace": "namespaces",
        "cap_id": "capabilities",
        "parent_cap_id": "capabilities",
        "issuer_cap_id": "capabilities",
        "capability_id": "capabilities",
        "tool_id": "tools",
        "registered_tool_id": "tools",
        "candidate_id": "candidates",
    }

    @classmethod
    def remap_row(cls, row: Mapping[str, Any], identities: SnapshotIdentityMap) -> dict[str, Any]:
        remapped = deepcopy(dict(row))
        for field_name, map_name in cls._FIELD_MAPS.items():
            value = remapped.get(field_name)
            selected_map = getattr(identities, map_name)
            if value is not None and str(value) in selected_map:
                remapped[field_name] = selected_map[str(value)]
        if "wait_state_json" in remapped and "outcome_json" in remapped:
            wait_state = remap_process_wait_state(
                process_wait_state_from_json(remapped["wait_state_json"]),
                pids=identities.pids,
                objects=identities.objects,
            )
            outcome = remap_process_outcome(
                process_outcome_from_json(remapped["outcome_json"]),
                objects=identities.objects,
            )
            remapped["wait_state_json"] = dumps(
                process_wait_state_to_mapping(wait_state)
            )
            remapped["outcome_json"] = dumps(process_outcome_to_mapping(outcome))
            remapped["status_message"] = legacy_status_message(
                wait_state,
                outcome,
                remapped.get("status_message"),
            )
        return remapped

    @classmethod
    def remap_rows(cls, rows: SnapshotRows, identities: SnapshotIdentityMap) -> SnapshotRows:
        return SnapshotRows(
            **{
                table: tuple(cls.remap_row(row, identities) for row in getattr(rows, table))
                for table in SnapshotRows.TABLES
            }
        )

    @classmethod
    def remap(cls, snapshot: ProcessSnapshot, identities: SnapshotIdentityMap) -> ProcessSnapshot:
        root_pid = identities.pids.get(snapshot.header.root_pid, snapshot.header.root_pid)
        return ProcessSnapshot(
            header=SnapshotHeader(
                schema_version=snapshot.header.schema_version,
                checkpoint_id=snapshot.header.checkpoint_id,
                root_pid=root_pid,
                reason=snapshot.header.reason,
                created_at=snapshot.header.created_at,
                created_by=snapshot.header.created_by,
            ),
            subtree_pids=tuple(identities.pids.get(pid, pid) for pid in snapshot.subtree_pids),
            object_oids=tuple(identities.objects.get(oid, oid) for oid in snapshot.object_oids),
            owned_object_oids=tuple(identities.objects.get(oid, oid) for oid in snapshot.owned_object_oids),
            referenced_object_oids=tuple(
                identities.objects.get(oid, oid) for oid in snapshot.referenced_object_oids
            ),
            referenced_object_types={
                identities.objects.get(oid, oid): object_type
                for oid, object_type in snapshot.referenced_object_types.items()
            },
            namespaces=tuple(identities.namespaces.get(name, name) for name in snapshot.namespaces),
            owned_namespaces=tuple(
                identities.namespaces.get(name, name) for name in snapshot.owned_namespaces
            ),
            rows=cls.remap_rows(snapshot.rows, identities),
            object_payloads={
                identities.objects.get(oid, oid): deepcopy(payload)
                for oid, payload in snapshot.object_payloads.items()
            },
            images=deepcopy(snapshot.images),
            image_artifacts=deepcopy(snapshot.image_artifacts),
            jit_sources={
                identities.tools.get(tool_id, tool_id): source
                for tool_id, source in snapshot.jit_sources.items()
            },
            modules=tuple(deepcopy(module) for module in snapshot.modules),
        )

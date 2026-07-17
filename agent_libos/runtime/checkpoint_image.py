from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from agent_libos.memory.object_memory import ObjectMemoryManager
from agent_libos.models import (
    AgentImage,
    DataLabels,
    ObjectHandle,
    ObjectLifecycleState,
    ObjectMetadata,
    ObjectOwnerKind,
    ToolSpec,
)
from agent_libos.models.exceptions import NotFound, ValidationError
from agent_libos.ports import AuditPort
from agent_libos.runtime.image_artifact import ImageArtifactLoader
from agent_libos.storage import RuntimeStore
from agent_libos.storage.repositories import ProcessRepository
from agent_libos.utils.ids import new_id, utc_now
from agent_libos.utils.serde import dumps, loads

if TYPE_CHECKING:
    from agent_libos.capability.manager import CapabilityManager
    from agent_libos.runtime.authority_manifest_manager import AuthorityManifestManager
    from agent_libos.runtime.checkpoint_manager import CheckpointManager
    from agent_libos.tools.broker import ToolBroker


class CheckpointImageInstaller:
    """Remap and install one checkpoint-commit artifact into a fresh process."""

    def __init__(
        self,
        *,
        loader: ImageArtifactLoader,
        store: RuntimeStore,
        processes: ProcessRepository,
        memory: ObjectMemoryManager,
        capabilities: CapabilityManager,
        authority_manifests: AuthorityManifestManager,
        checkpoint: CheckpointManager,
        tools: ToolBroker,
        audit: AuditPort,
    ) -> None:
        self._loader = loader
        self._store = store
        self._processes = processes
        self._memory = memory
        self._capabilities = capabilities
        self._authority_manifests = authority_manifests
        self._checkpoint = checkpoint
        self._tools = tools
        self._audit = audit

    def preflight(self, image: AgentImage) -> None:
        artifact = self._loader.load(
            image,
            expected_kind="checkpoint_commit",
        )
        self._checkpoint.require_snapshot_modules(
            {"modules": artifact.get("modules", [])}
        )

    def install(self, pid: str, image: AgentImage) -> None:
        artifact = self._loader.load(
            image,
            expected_kind="checkpoint_commit",
        )
        self._checkpoint.require_snapshot_modules(
            {"modules": artifact.get("modules", [])}
        )
        remapped = self._remap_for_process(pid, artifact)
        self._assert_data_flow(pid, remapped)
        self._insert_memory_rows(remapped)
        tool_table = self._restore_tool_table(pid, artifact)
        process = self._require_process(pid)
        process.working_directory = str(
            artifact.get("working_directory")
            or process.working_directory
            or "."
        )
        process.loaded_skills = self._remap_loaded_skills(
            artifact.get("loaded_skills", {}),
            tool_table,
        )
        process.tool_table = tool_table
        self._merge_memory_view(process, artifact, remapped)
        process.updated_at = utc_now()
        self._processes.patch_process(
            pid,
            {
                "working_directory": process.working_directory,
                "loaded_skills": process.loaded_skills,
                "tool_table": process.tool_table,
                "memory_view": process.memory_view,
                "updated_at": process.updated_at,
            },
            expected_revision=process.revision,
        )
        self._audit.record(
            actor=f"image:{image.image_id}",
            action="image.boot.checkpoint_commit",
            target=f"process:{pid}",
            decision={
                "image_id": image.image_id,
                "artifact_id": image.boot.get("artifact_id"),
                "source_checkpoint_id": artifact.get("source_checkpoint_id"),
                "objects": len(remapped["object_payloads"]),
                "tools": sorted(tool_table),
            },
        )

    def _remap_for_process(
        self,
        pid: str,
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        source_pid = str(artifact["source_pid"])
        old_oids = list(artifact.get("object_oids", []))
        namespace_map = {
            namespace: self._remap_namespace(pid, source_pid, namespace)
            for namespace in artifact.get("namespaces", [])
        }
        source_object_rows = [
            row
            for row in artifact.get("rows", {}).get("objects", [])
            if row["oid"] in old_oids
        ]
        oid_map, reused_oids = self._object_oid_map(
            pid,
            source_object_rows,
            namespace_map,
        )
        capability_rows = artifact.get("rows", {}).get("capabilities", [])
        capability_map = {
            row["cap_id"]: new_id("cap")
            for row in capability_rows
        }
        now = utc_now()
        return {
            "oid_map": oid_map,
            "namespace_map": namespace_map,
            "capability_map": capability_map,
            "object_namespaces": [
                self._remap_namespace_row(row, pid, namespace_map, now)
                for row in artifact.get("rows", {}).get("object_namespaces", [])
                if row["namespace"] in namespace_map
            ],
            "objects": [
                self._remap_object_row(row, pid, oid_map, namespace_map, now)
                for row in source_object_rows
                if row["oid"] not in reused_oids
            ],
            "object_links": [
                self._remap_link_row(row, oid_map, now)
                for row in artifact.get("rows", {}).get("object_links", [])
                if row["src_oid"] in oid_map and row["dst_oid"] in oid_map
            ],
            "capabilities": [
                self._remap_capability_row(
                    row,
                    pid,
                    oid_map,
                    namespace_map,
                    capability_map,
                    now,
                )
                for row in capability_rows
                if row["subject"] == source_pid
            ],
            "object_payloads": {
                oid_map[oid]: deepcopy(payload)
                for oid, payload in artifact.get("object_payloads", {}).items()
                if oid in oid_map and oid not in reused_oids
            },
        }

    def _object_oid_map(
        self,
        pid: str,
        rows: list[dict[str, Any]],
        namespace_map: dict[str, str],
    ) -> tuple[dict[str, str], set[str]]:
        oid_map: dict[str, str] = {}
        reused: set[str] = set()
        for row in rows:
            old_oid = str(row["oid"])
            name = str(row.get("name") or old_oid)
            namespace = namespace_map.get(str(row["namespace"]), str(row["namespace"]))
            # Anonymous/default names are remapped with their OID and cannot
            # collide. Stable user names prefer a live object already owned by
            # this process, preserving current memory during exec.
            existing = None
            if name != old_oid:
                existing = self._store.get_object_by_name(name, namespace)
            if existing is None:
                oid_map[old_oid] = new_id("obj")
                continue
            if (
                existing.owner_kind != ObjectOwnerKind.PROCESS
                or existing.owner_id != pid
                or existing.lifecycle_state != ObjectLifecycleState.LIVE
            ):
                raise ValidationError(
                    f"committed image object name conflicts with non-process state: "
                    f"{namespace}/{name}"
                )
            oid_map[old_oid] = existing.oid
            reused.add(old_oid)
        return oid_map, reused

    def _remap_namespace(
        self,
        pid: str,
        source_pid: str,
        namespace: str,
    ) -> str:
        if namespace == self._memory.process_namespace(source_pid):
            return self._memory.process_namespace(pid)
        return f"image_commit/{pid}/{namespace}"

    def _remap_namespace_row(
        self,
        row: dict[str, Any],
        pid: str,
        namespace_map: dict[str, str],
        now: str,
    ) -> dict[str, Any]:
        item = dict(row)
        item["namespace"] = namespace_map[item["namespace"]]
        if item.get("parent_namespace") in namespace_map:
            item["parent_namespace"] = namespace_map[item["parent_namespace"]]
        elif item["namespace"] == self._memory.process_namespace(pid):
            item["parent_namespace"] = None
        item["created_by"] = pid
        metadata = loads(item.get("metadata_json"), {})
        if metadata.get("kind") == "process":
            metadata["pid"] = pid
        item["metadata_json"] = dumps(metadata)
        item["updated_at"] = now
        return item

    def _remap_object_row(
        self,
        row: dict[str, Any],
        pid: str,
        oid_map: dict[str, str],
        namespace_map: dict[str, str],
        now: str,
    ) -> dict[str, Any]:
        item = dict(row)
        old_oid = item["oid"]
        item["oid"] = oid_map[old_oid]
        if item.get("name") == old_oid:
            item["name"] = item["oid"]
        item["namespace"] = namespace_map.get(
            item["namespace"],
            item["namespace"],
        )
        item["created_by"] = pid
        item["owner_kind"] = ObjectOwnerKind.PROCESS.value
        item["owner_id"] = pid
        item["lifecycle_state"] = "live"
        item["deleted_at"] = None
        provenance = loads(item.get("provenance_json"), {})
        provenance["parent_oids"] = [
            oid_map.get(oid, oid)
            for oid in provenance.get("parent_oids", [])
        ]
        item["provenance_json"] = dumps(provenance)
        item["payload_json"] = dumps(self._store.payload_marker(present=False))
        item["created_at"] = now
        item["updated_at"] = now
        return item

    @staticmethod
    def _remap_link_row(
        row: dict[str, Any],
        oid_map: dict[str, str],
        now: str,
    ) -> dict[str, Any]:
        item = dict(row)
        item["id"] = new_id("link")
        item["src_oid"] = oid_map[item["src_oid"]]
        item["dst_oid"] = oid_map[item["dst_oid"]]
        item["created_at"] = now
        return item

    @staticmethod
    def _remap_capability_row(
        row: dict[str, Any],
        pid: str,
        oid_map: dict[str, str],
        namespace_map: dict[str, str],
        capability_map: dict[str, str],
        now: str,
    ) -> dict[str, Any]:
        item = dict(row)
        item["cap_id"] = capability_map[item["cap_id"]]
        item["subject"] = pid
        item["issuer_cap_id"] = (
            capability_map.get(item.get("issuer_cap_id"))
            if item.get("issuer_cap_id")
            else None
        )
        item["parent_cap_id"] = (
            capability_map.get(item.get("parent_cap_id"))
            if item.get("parent_cap_id")
            else None
        )
        resource = str(item["resource"])
        if resource.startswith("object:"):
            item["resource"] = f"object:{oid_map[resource.split(':', 1)[1]]}"
        elif resource.startswith("object_namespace:"):
            namespace = resource.split(":", 1)[1]
            item["resource"] = f"object_namespace:{namespace_map[namespace]}"
        item["issued_by"] = f"image.commit:{item['issued_by']}"
        item["issued_at"] = now
        return item

    def _insert_memory_rows(self, remapped: dict[str, Any]) -> None:
        with self._store.transaction(include_object_payloads=True) as cursor:
            for row in remapped["object_namespaces"]:
                exists = cursor.execute(
                    "SELECT 1 FROM object_namespaces WHERE namespace = ?",
                    (row["namespace"],),
                ).fetchone()
                if exists is None:
                    self._store.insert_table_row("object_namespaces", row)
            for row in remapped["objects"]:
                item = dict(row)
                oid = str(item["oid"])
                if oid in remapped["object_payloads"]:
                    item["payload_json"] = dumps(remapped["object_payloads"][oid])
                self._store.insert_table_row("objects", item)
                if oid in remapped["object_payloads"]:
                    self._store.set_object_payload(
                        oid,
                        deepcopy(remapped["object_payloads"][oid]),
                    )
            for table in ("object_links", "capabilities"):
                for row in remapped[table]:
                    self._store.insert_table_row(table, row)

    def _assert_data_flow(self, pid: str, remapped: dict[str, Any]) -> None:
        for row in remapped["objects"]:
            oid = str(row.get("oid") or "")
            try:
                metadata = ObjectMetadata.from_persisted(
                    loads(row.get("metadata_json"), {})
                )
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"invalid committed image Object metadata for {oid}: {exc}"
                ) from exc
            self._authority_manifests.assert_data_flow_labels(
                pid,
                DataLabels.from_object_metadata(metadata),
            )

    def _restore_tool_table(
        self,
        pid: str,
        artifact: dict[str, Any],
    ) -> dict[str, str]:
        tool_rows = {
            row["tool_id"]: row
            for row in artifact.get("rows", {}).get("tools", [])
        }
        old_to_new: dict[str, str] = {}
        table: dict[str, str] = {}
        jit_sources = artifact.get("jit_sources", {})
        for name, old_tool_id in artifact.get("tool_table", {}).items():
            if old_tool_id in jit_sources:
                handle = self._install_jit_tool(
                    pid,
                    old_tool_id,
                    tool_rows,
                    jit_sources,
                )
            else:
                handle = self._tools.resolve(name)
            old_to_new[old_tool_id] = handle.tool_id
            table[name] = handle.tool_id
        artifact["_tool_id_map"] = old_to_new
        return table

    def _install_jit_tool(
        self,
        pid: str,
        old_tool_id: str,
        tool_rows: dict[str, dict[str, Any]],
        jit_sources: dict[str, str],
    ) -> Any:
        row = tool_rows.get(old_tool_id)
        if row is None:
            raise RuntimeError(f"committed JIT tool row is missing: {old_tool_id}")
        return self._tools.install_committed_jit(
            pid,
            name=str(row["name"]),
            scope=str(row["scope"]),
            spec=ToolSpec(**loads(row["spec_json"], {})),
            source_code=str(jit_sources[old_tool_id]),
            registered_by=f"image.commit:{pid}",
        )

    @staticmethod
    def _remap_loaded_skills(
        loaded_skills: dict[str, Any],
        tool_table: dict[str, str],
    ) -> dict[str, Any]:
        updated = deepcopy(loaded_skills or {})
        for loaded in updated.values():
            if not isinstance(loaded, dict):
                continue
            for key in (
                "tool_ids",
                "jit_tool_ids",
                "base_tool_ids",
                "base_model_tool_ids",
            ):
                mapping = loaded.get(key)
                if isinstance(mapping, dict):
                    loaded[key] = {
                        name: tool_table[name]
                        for name in mapping
                        if name in tool_table
                    }
        return updated

    def _merge_memory_view(
        self,
        process: Any,
        artifact: dict[str, Any],
        remapped: dict[str, Any],
    ) -> None:
        source = loads(
            artifact.get("source_process", {}).get("memory_view_json"),
            {},
        )
        if not source:
            return
        existing_roots = (
            list(process.memory_view.roots)
            if process.memory_view is not None
            else []
        )
        roots = self._remap_memory_roots(process, source, remapped)
        for handle in existing_roots:
            if all(item.oid != handle.oid for item in roots):
                roots.append(handle)
        if process.memory_view is None:
            process.memory_view = self._memory.create_view(
                process.pid,
                roots,
                mode="mutable",
            )
        else:
            process.memory_view.roots = roots

    def _remap_memory_roots(
        self,
        process: Any,
        source: dict[str, Any],
        remapped: dict[str, Any],
    ) -> list[ObjectHandle]:
        roots: list[ObjectHandle] = []
        capability_map = remapped["capability_map"]
        oid_map = remapped["oid_map"]
        for root in source.get("roots", []):
            old_oid = root.get("oid")
            if old_oid not in oid_map:
                continue
            new_oid = oid_map[old_oid]
            rights = set(root.get("rights", []))
            new_capability = capability_map.get(root.get("capability_id"))
            if new_capability is None:
                handle = self._capabilities.handle_for_object(
                    subject=process.pid,
                    oid=new_oid,
                    rights=rights,
                    issued_by="image.commit",
                )
                new_capability = handle.capability_id
            roots.append(
                ObjectHandle(
                    oid=new_oid,
                    rights=rights,
                    capability_id=new_capability,
                    expires_at=root.get("expires_at"),
                )
            )
        return roots

    def _require_process(self, pid: str) -> Any:
        process = self._processes.get_process(pid)
        if process is None:
            raise NotFound(f"process not found: {pid}")
        return process


__all__ = ["CheckpointImageInstaller"]

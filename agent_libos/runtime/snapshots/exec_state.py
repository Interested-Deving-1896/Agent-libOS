from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from agent_libos.memory.object_memory import ObjectMemoryManager
from agent_libos.models import CapabilityStatus, ObjectOwnerKind, ProcessStatus
from agent_libos.models.exceptions import NotFound
from agent_libos.runtime.snapshots.codec import SnapshotCodec
from agent_libos.runtime.snapshots.models import (
    ExecRollbackState,
    SNAPSHOT_SCHEMA_VERSION,
)
from agent_libos.storage import RuntimeStore
from agent_libos.utils.ids import utc_now
from agent_libos.utils.serde import dumps, loads

if TYPE_CHECKING:
    from agent_libos.tools.broker import ToolBroker


class ProcessExecStateService:
    """Capture and atomically restore the process-local state changed by exec."""

    def __init__(
        self,
        store: RuntimeStore,
        memory: ObjectMemoryManager,
        tools: ToolBroker,
    ) -> None:
        self._store = store
        self._memory = memory
        self._tools = tools

    def capture(self, pid: str) -> ExecRollbackState:
        process_rows = self._store.select_table_rows(
            "processes",
            "pid = ?",
            (pid,),
        )
        if not process_rows:
            raise NotFound(f"process not found: {pid}")
        object_rows = self._owned_object_rows(pid)
        object_oids = [str(row["oid"]) for row in object_rows]
        namespace_rows = self._owned_namespace_rows(pid)
        tool_ids = set(loads(process_rows[0].get("tool_table_json"), {}).values())
        tool_handles, jit_sources = self._tools.snapshot_loaded_tool_state(tool_ids)
        created_at = utc_now()
        snapshot = SnapshotCodec.decode_mapping(
            {
                "version": SNAPSHOT_SCHEMA_VERSION,
                "checkpoint_id": f"exec:{pid}:{created_at}",
                "pid": pid,
                "reason": "process.exec rollback",
                "created_at": created_at,
                "created_by": "runtime.process.exec",
                "subtree_pids": [pid],
                "object_oids": object_oids,
                "owned_object_oids": object_oids,
                "referenced_object_oids": [],
                "referenced_object_types": {},
                "namespaces": [str(row["namespace"]) for row in namespace_rows],
                "owned_namespaces": [
                    str(row["namespace"])
                    for row in namespace_rows
                ],
                "rows": self._captured_rows(
                    pid,
                    process_rows,
                    object_rows,
                    object_oids,
                    namespace_rows,
                ),
                "object_payloads": self._store.snapshot_object_payloads(object_oids),
                "images": {},
                "image_artifacts": {},
                "jit_sources": jit_sources,
                "modules": [],
            }
        )
        return ExecRollbackState(
            snapshot=snapshot,
            tool_ids=frozenset(str(tool_id) for tool_id in tool_ids),
            tool_handles=tool_handles,
        )

    def restore(
        self,
        state: ExecRollbackState,
        *,
        fence_execution: bool = True,
    ) -> None:
        snapshot = state.snapshot
        pid = snapshot.header.root_pid
        current_process = self._store.get_process(pid)
        if current_process is None:
            raise NotFound(f"process not found during exec compensation: {pid}")
        expected_process_revision = current_process.revision
        before_process_rows = list(snapshot.rows.processes)
        if len(before_process_rows) != 1:
            raise NotFound(f"exec rollback snapshot has no unique process row: {pid}")
        before_process_row = dict(before_process_rows[0])
        current_object_oids = [
            str(row["oid"])
            for row in self._owned_object_rows(pid)
        ]
        before_object_oids = set(snapshot.owned_object_oids)
        current_object_oid_set = set(current_object_oids)
        borrowed_new_oids = self._externally_borrowed_oids(
            pid,
            current_object_oid_set - before_object_oids,
        )
        cleanup_object_oids = (current_object_oid_set - before_object_oids) - borrowed_new_oids
        object_oids = sorted(before_object_oids | cleanup_object_oids)
        namespace_names = sorted(
            set(snapshot.owned_namespaces)
            | {
                str(row["namespace"])
                for row in self._owned_namespace_rows(pid)
            }
        )
        current_tool_ids = (
            set(current_process.tool_table.values())
            if current_process is not None
            else set()
        )
        stale_tool_ids = current_tool_ids - set(state.tool_ids)
        process_restored = False
        with self._store.transaction(include_object_payloads=True) as cursor:
            self._delete_current_scope(
                cursor,
                pid,
                object_oids,
                namespace_names,
            )
            self._insert_snapshot_rows(snapshot)
            self._remove_exec_created_capabilities(
                cursor,
                pid=pid,
                before_rows=list(snapshot.rows.capabilities),
                cleanup_object_oids=cleanup_object_oids,
            )
            current_capability_ids = [
                str(row["cap_id"])
                for row in cursor.execute(
                    "SELECT cap_id FROM capabilities WHERE subject = ? ORDER BY cap_id",
                    (pid,),
                )
            ]
            process_restored = self._store.restore_process_for_exec(
                before_process_row,
                expected_revision=expected_process_revision,
                capability_ids=current_capability_ids,
                fence_execution=fence_execution,
            )
        self._prune_stale_tools(stale_tool_ids, pid)
        self._tools.restore_loaded_jit_state(
            state.tool_handles,
            dict(snapshot.jit_sources),
        )
        if not process_restored:
            latest = self._store.get_process(pid)
            if latest is not None and latest.status not in {
                ProcessStatus.EXITED,
                ProcessStatus.FAILED,
                ProcessStatus.KILLED,
            }:
                self._store.transition_process(
                    pid,
                    ProcessStatus.PAUSED,
                    expected_revision=latest.revision,
                    status_message="exec_recovery_conflict",
                )

    def _captured_rows(
        self,
        pid: str,
        process_rows: list[dict[str, Any]],
        object_rows: list[dict[str, Any]],
        object_oids: list[str],
        namespace_rows: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            "processes": process_rows,
            "object_namespaces": namespace_rows,
            "objects": object_rows,
            "object_links": self._object_link_rows(object_oids),
            "capabilities": self._store.select_table_rows(
                "capabilities",
                "subject = ?",
                (pid,),
                order_by="cap_id",
            ),
            "process_messages": [],
            "llm_pending_actions": self._store.select_table_rows(
                "llm_pending_actions",
                "pid = ?",
                (pid,),
            ),
            "skills": [],
            "tools": [],
            "tool_candidates": self._store.select_table_rows(
                "tool_candidates",
                "pid = ?",
                (pid,),
                order_by="candidate_id",
            ),
            "process_resource_reservations": self._store.select_table_rows(
                "process_resource_reservations",
                "parent_pid = ? OR child_pid = ?",
                (pid, pid),
                order_by="parent_pid, child_pid",
            ),
        }

    def _delete_current_scope(
        self,
        cursor: Any,
        pid: str,
        object_oids: list[str],
        namespace_names: list[str],
    ) -> None:
        if object_oids:
            placeholders = ", ".join("?" for _ in object_oids)
            cursor.execute(
                f"DELETE FROM object_links WHERE src_oid IN ({placeholders}) "
                f"OR dst_oid IN ({placeholders})",
                [*object_oids, *object_oids],
            )
            cursor.execute(
                f"DELETE FROM objects WHERE oid IN ({placeholders})",
                object_oids,
            )
            for oid in object_oids:
                self._store.forget_object_payload(oid)
        if namespace_names:
            placeholders = ", ".join("?" for _ in namespace_names)
            cursor.execute(
                f"DELETE FROM object_namespaces WHERE namespace IN ({placeholders})",
                namespace_names,
            )
        cursor.execute("DELETE FROM llm_pending_actions WHERE pid = ?", (pid,))
        cursor.execute("DELETE FROM tool_candidates WHERE pid = ?", (pid,))

    def _insert_snapshot_rows(self, snapshot: Any) -> None:
        tables = snapshot.rows.to_mapping()
        for row in tables["object_namespaces"]:
            self._store.insert_table_row("object_namespaces", row)
        for row in tables["objects"]:
            item = dict(row)
            oid = str(item["oid"])
            if oid in snapshot.object_payloads:
                item["payload_json"] = dumps(snapshot.object_payloads[oid])
            else:
                item["payload_json"] = dumps(
                    self._store.payload_marker(present=False)
                )
            self._store.insert_table_row("objects", item)
            if oid in snapshot.object_payloads:
                self._store.set_object_payload(
                    oid,
                    deepcopy(snapshot.object_payloads[oid]),
                )
        for table in (
            "object_links",
            "llm_pending_actions",
            "tool_candidates",
        ):
            for row in tables[table]:
                self._store.insert_table_row(table, row)

    def _externally_borrowed_oids(self, pid: str, oids: set[str]) -> set[str]:
        if not oids:
            return set()
        resources = [f"object:{oid}" for oid in sorted(oids)]
        placeholders = ", ".join("?" for _ in resources)
        rows = self._store.select_table_rows(
            "capabilities",
            f"resource IN ({placeholders}) AND subject <> ? AND status = ?",
            [*resources, pid, CapabilityStatus.ACTIVE.value],
        )
        return {
            str(row["resource"]).split(":", 1)[1]
            for row in rows
            if str(row.get("resource", "")).startswith("object:")
        }

    def _remove_exec_created_capabilities(
        self,
        cursor: Any,
        *,
        pid: str,
        before_rows: list[dict[str, Any]],
        cleanup_object_oids: set[str],
    ) -> None:
        before_ids = {str(row["cap_id"]) for row in before_rows}
        current_rows = list(
            cursor.execute("SELECT * FROM capabilities WHERE subject = ?", (pid,))
        )
        cleanup_resources = {f"object:{oid}" for oid in cleanup_object_oids}
        for row in current_rows:
            cap_id = str(row["cap_id"])
            if cap_id in before_ids or str(row["status"]) != CapabilityStatus.ACTIVE.value:
                continue
            issued_by = str(row["issued_by"])
            rollback_owned = issued_by.startswith(
                ("process.exec", "image:", "checkpoint.image", "image_package")
            ) or str(row["resource"]) in cleanup_resources
            if not rollback_owned:
                continue
            cursor.execute(
                "DELETE FROM capabilities WHERE cap_id = ? AND subject = ? AND status = ?",
                (cap_id, pid, CapabilityStatus.ACTIVE.value),
            )

    def _owned_object_rows(self, pid: str) -> list[dict[str, Any]]:
        return self._store.select_table_rows(
            "objects",
            "owner_kind = ? AND owner_id = ? AND lifecycle_state = ?",
            (ObjectOwnerKind.PROCESS.value, pid, "live"),
            order_by="oid",
        )

    def _owned_namespace_rows(self, pid: str) -> list[dict[str, Any]]:
        return self._store.select_table_rows(
            "object_namespaces",
            "created_by = ? OR namespace = ?",
            (pid, self._memory.process_namespace(pid)),
            order_by="namespace",
        )

    def _object_link_rows(self, object_oids: list[str]) -> list[dict[str, Any]]:
        if not object_oids:
            return []
        placeholders = ", ".join("?" for _ in object_oids)
        return self._store.select_table_rows(
            "object_links",
            f"src_oid IN ({placeholders}) OR dst_oid IN ({placeholders})",
            [*object_oids, *object_oids],
            order_by="id",
        )

    def _prune_stale_tools(self, tool_ids: set[str], pid: str) -> None:
        for tool_id in tool_ids:
            if self._tool_id_used_by_other_process(tool_id, pid):
                continue
            self._tools.forget_loaded_jit(tool_id)
            self._store.delete_tool(tool_id)

    def _tool_id_used_by_other_process(self, tool_id: str, pid: str) -> bool:
        return any(
            process.pid != pid and tool_id in process.tool_table.values()
            for process in self._store.list_processes()
        )


__all__ = ["ProcessExecStateService"]

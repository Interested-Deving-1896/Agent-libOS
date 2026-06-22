from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models.exceptions import ValidationError
from agent_libos.utils.ids import utc_now
from agent_libos.models import (
    AgentObject,
    AgentImage,
    AgentProcess,
    AuditRecord,
    Capability,
    CapabilityEffect,
    CapabilityStatus,
    Checkpoint,
    Event,
    EventPriority,
    EventType,
    ExternalEffectRecord,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
    HumanRequest,
    HumanRequestStatus,
    JsonRpcEndpointSpec,
    JsonRpcHeaderSpec,
    JsonRpcMethodSpec,
    LLMCallRecord,
    MemoryView,
    ObjectFilter,
    ObjectHandle,
    ObjectLink,
    ObjectMetadata,
    ObjectNamespace,
    ObjectTask,
    ObjectTaskNotification,
    ObjectTaskNotificationStatus,
    ObjectTaskOwnerWatch,
    ObjectTaskStatus,
    ObjectType,
    ProcessStatus,
    ProcessMessage,
    ProcessMessageKind,
    ProcessMessageStatus,
    Provenance,
    RelationType,
    ResourceBudget,
    ResourceReservation,
    ResourceUsage,
    ToolCandidate,
    ToolCandidateStatus,
    ToolHandle,
    ToolSpec,
    ViewMode,
)
from agent_libos.skills.schema import ActionSchema, JitToolSpec, SkillPackage, SkillResource
from agent_libos.utils.serde import dumps, loads

_LLM_DEFAULTS = DEFAULT_CONFIG.llm


class SQLiteStore:
    """Small SQLite repository used by the MVP runtime.

    The store is intentionally thin: policy, permissions, and process semantics
    live in managers. This layer only owns durable shape and reconstruction.
    """

    SYSTEM_NAMESPACE = "system"

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # Object payloads are runtime memory, not durable database state. SQLite
        # stores only metadata plus a marker saying whether a payload was present
        # in this process.
        self._object_payloads: dict[str, Any] = {}
        self.initialize()

    def close(self) -> None:
        self.conn.close()

    def initialize(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS objects (
                  oid TEXT PRIMARY KEY,
                  namespace TEXT NOT NULL DEFAULT 'system',
                  name TEXT NOT NULL,
                  type TEXT NOT NULL,
                  schema_version TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  metadata_json TEXT NOT NULL,
                  provenance_json TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  immutable INTEGER NOT NULL,
                  created_by TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS object_namespaces (
                  namespace TEXT PRIMARY KEY,
                  parent_namespace TEXT,
                  metadata_json TEXT NOT NULL,
                  created_by TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS object_links (
                  id TEXT PRIMARY KEY,
                  src_oid TEXT NOT NULL,
                  relation TEXT NOT NULL,
                  dst_oid TEXT NOT NULL,
                  metadata_json TEXT NOT NULL,
                  created_by TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processes (
                  pid TEXT PRIMARY KEY,
                  parent_pid TEXT,
                  image_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  goal_oid TEXT,
                  memory_view_json TEXT,
                  capabilities_json TEXT NOT NULL,
                  loaded_skills_json TEXT NOT NULL,
                  tool_table_json TEXT NOT NULL,
                  event_cursor TEXT,
                  checkpoint_head TEXT,
                  status_message TEXT,
                  resource_budget_json TEXT NOT NULL,
                  resource_usage_json TEXT NOT NULL DEFAULT '{}',
                  working_directory TEXT NOT NULL DEFAULT '.',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS process_resource_reservations (
                  parent_pid TEXT NOT NULL,
                  child_pid TEXT NOT NULL,
                  reservation_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(parent_pid, child_pid)
                );

                CREATE INDEX IF NOT EXISTS idx_resource_reservations_parent
                  ON process_resource_reservations(parent_pid, child_pid);

                CREATE INDEX IF NOT EXISTS idx_resource_reservations_child
                  ON process_resource_reservations(child_pid, parent_pid);

                CREATE TABLE IF NOT EXISTS events (
                  event_id TEXT PRIMARY KEY,
                  type TEXT NOT NULL,
                  source TEXT NOT NULL,
                  target TEXT,
                  payload_json TEXT NOT NULL,
                  priority TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  correlation_id TEXT,
                  causality_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS capabilities (
                  cap_id TEXT PRIMARY KEY,
                  subject TEXT NOT NULL,
                  resource TEXT NOT NULL,
                  rights_json TEXT NOT NULL,
                  constraints_json TEXT NOT NULL,
                  issued_by TEXT NOT NULL,
                  issued_at TEXT NOT NULL,
                  expires_at TEXT,
                  delegable INTEGER NOT NULL,
                  revocable INTEGER NOT NULL,
                  effect TEXT NOT NULL,
                  issuer_cap_id TEXT,
                  parent_cap_id TEXT,
                  delegation_depth INTEGER NOT NULL,
                  max_delegation_depth INTEGER,
                  uses_remaining INTEGER,
                  status TEXT NOT NULL,
                  metadata_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_records (
                  record_id TEXT PRIMARY KEY,
                  timestamp TEXT NOT NULL,
                  actor TEXT NOT NULL,
                  action TEXT NOT NULL,
                  target TEXT,
                  input_refs_json TEXT NOT NULL,
                  output_refs_json TEXT NOT NULL,
                  capability_refs_json TEXT NOT NULL,
                  decision_json TEXT,
                  correlation_id TEXT,
                  parent_record_id TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_audit_records_created
                  ON audit_records(timestamp, record_id);

                CREATE INDEX IF NOT EXISTS idx_audit_records_actor_created
                  ON audit_records(actor, timestamp, record_id);

                CREATE INDEX IF NOT EXISTS idx_audit_records_target_created
                  ON audit_records(target, timestamp, record_id);

                CREATE INDEX IF NOT EXISTS idx_audit_records_correlation_created
                  ON audit_records(correlation_id, timestamp, record_id);

                CREATE TABLE IF NOT EXISTS external_effects (
                  effect_id TEXT PRIMARY KEY,
                  record_id TEXT,
                  event_id TEXT,
                  pid TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  operation TEXT NOT NULL,
                  target TEXT,
                  rollback_class TEXT NOT NULL,
                  rollback_status TEXT NOT NULL,
                  state_mutation INTEGER NOT NULL,
                  information_flow INTEGER NOT NULL,
                  provider_metadata_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_external_effects_created
                  ON external_effects(created_at, effect_id);

                CREATE INDEX IF NOT EXISTS idx_external_effects_pid_created
                  ON external_effects(pid, created_at, effect_id);

                CREATE TABLE IF NOT EXISTS checkpoints (
                  checkpoint_id TEXT PRIMARY KEY,
                  pid TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  snapshot_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  created_by TEXT,
                  snapshot_version INTEGER NOT NULL DEFAULT 1,
                  metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS human_requests (
                  request_id TEXT PRIMARY KEY,
                  pid TEXT NOT NULL,
                  human TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  decision_json TEXT,
                  blocking INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS llm_calls (
                  call_id TEXT PRIMARY KEY,
                  pid TEXT,
                  image_id TEXT,
                  purpose TEXT NOT NULL,
                  status TEXT NOT NULL,
                  api TEXT,
                  model TEXT,
                  request_id TEXT,
                  response_id TEXT,
                  messages_json TEXT NOT NULL,
                  tools_json TEXT NOT NULL,
                  request_options_json TEXT NOT NULL,
                  response_content TEXT NOT NULL,
                  tool_calls_json TEXT NOT NULL,
                  reasoning_json TEXT,
                  usage_json TEXT NOT NULL,
                  raw_response_json TEXT,
                  observability_json TEXT NOT NULL DEFAULT '{}',
                  error TEXT,
                  created_at TEXT NOT NULL,
                  completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_llm_calls_pid_created
                  ON llm_calls(pid, created_at);

                CREATE INDEX IF NOT EXISTS idx_llm_calls_request_id
                  ON llm_calls(request_id);

                CREATE INDEX IF NOT EXISTS idx_llm_calls_response_id
                  ON llm_calls(response_id);

                CREATE TABLE IF NOT EXISTS llm_pending_actions (
                  pid TEXT PRIMARY KEY,
                  wait_type TEXT NOT NULL,
                  request_id TEXT,
                  child_pid TEXT,
                  filters_json TEXT NOT NULL,
                  action_json TEXT NOT NULL,
                  content_preview TEXT NOT NULL,
                  tool_call_count INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS process_messages (
                  message_id TEXT PRIMARY KEY,
                  sender TEXT NOT NULL,
                  recipient_pid TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  channel TEXT NOT NULL DEFAULT 'default',
                  correlation_id TEXT,
                  reply_to TEXT,
                  subject TEXT NOT NULL,
                  body TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  acked_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_process_messages_recipient_status_kind
                  ON process_messages(recipient_pid, status, kind, channel, created_at);

                CREATE INDEX IF NOT EXISTS idx_process_messages_correlation
                  ON process_messages(recipient_pid, correlation_id, status, created_at);

                CREATE TABLE IF NOT EXISTS object_tasks (
                  task_id TEXT PRIMARY KEY,
                  owner_oid TEXT NOT NULL,
                  creator_pid TEXT NOT NULL,
                  runner_pid TEXT,
                  tool TEXT NOT NULL,
                  tool_id TEXT,
                  status TEXT NOT NULL,
                  notification_json TEXT NOT NULL,
                  owner_watch_json TEXT NOT NULL,
                  result_oid TEXT,
                  error TEXT,
                  wait_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  started_at TEXT,
                  completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_object_tasks_owner_status
                  ON object_tasks(owner_oid, status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_object_tasks_creator_status
                  ON object_tasks(creator_pid, status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_object_tasks_runner
                  ON object_tasks(runner_pid);

                CREATE TABLE IF NOT EXISTS skills (
                  skill_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  version TEXT NOT NULL,
                  package_json TEXT NOT NULL,
                  source_type TEXT NOT NULL,
                  source TEXT,
                  package_sha256 TEXT NOT NULL,
                  registered_by TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_skills_name
                  ON skills(name);

                CREATE TABLE IF NOT EXISTS skill_trust (
                  trust_id TEXT PRIMARY KEY,
                  source_type TEXT NOT NULL,
                  source TEXT NOT NULL,
                  package_sha256 TEXT NOT NULL,
                  trusted_by TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  metadata_json TEXT NOT NULL,
                  UNIQUE(source_type, source, package_sha256)
                );

                CREATE TABLE IF NOT EXISTS jsonrpc_endpoints (
                  endpoint_id TEXT PRIMARY KEY,
                  spec_json TEXT NOT NULL,
                  registered_by TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS images (
                  image_id TEXT PRIMARY KEY,
                  manifest_json TEXT NOT NULL,
                  registered_by TEXT NOT NULL,
                  source TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS image_artifacts (
                  artifact_id TEXT PRIMARY KEY,
                  kind TEXT NOT NULL,
                  artifact_json TEXT NOT NULL,
                  sha256 TEXT NOT NULL,
                  created_by TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  metadata_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tools (
                  tool_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  spec_json TEXT NOT NULL,
                  scope TEXT NOT NULL,
                  registered_by TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  ephemeral INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_candidates (
                  candidate_id TEXT PRIMARY KEY,
                  pid TEXT NOT NULL,
                  spec_json TEXT NOT NULL,
                  source_code TEXT NOT NULL,
                  tests_json TEXT NOT NULL,
                  requested_capabilities_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  registered_tool_id TEXT,
                  validation_json TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_modules (
                  module_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  version TEXT NOT NULL,
                  entrypoint TEXT NOT NULL,
                  manifest_path TEXT NOT NULL,
                  manifest_sha256 TEXT NOT NULL,
                  source_path TEXT NOT NULL,
                  source_sha256 TEXT NOT NULL,
                  status TEXT NOT NULL,
                  loaded_at TEXT,
                  registered_json TEXT NOT NULL,
                  error TEXT,
                  metadata_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_object_namespace_schema()
            self._ensure_process_schema()
            self._ensure_resource_reservation_schema()
            self._ensure_capability_schema()
            self._ensure_audit_schema()
            self._ensure_llm_call_schema()
            self._ensure_process_message_schema()
            self._ensure_object_task_schema()
            self._ensure_checkpoint_schema()
            self._ensure_external_effect_schema()
            self._ensure_skill_schema()
            self._ensure_tool_candidate_schema()
            self._ensure_jsonrpc_endpoint_schema()
            self._ensure_runtime_module_schema()
            self.conn.commit()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, tuple(params)))

    @contextmanager
    def transaction(self, *, include_object_payloads: bool = False):
        """Run direct SQL mutations atomically.

        Object payloads live outside SQLite, so callers that change object rows
        and payloads together must ask for an in-memory payload rollback too.
        """

        with self._lock:
            payloads = deepcopy(self._object_payloads) if include_object_payloads else None
            try:
                self.conn.execute("BEGIN")
                yield self.conn.cursor()
            except Exception:
                self.conn.rollback()
                if payloads is not None:
                    self._object_payloads = payloads
                raise
            else:
                self.conn.commit()

    def insert_object(self, obj: AgentObject) -> None:
        with self.transaction(include_object_payloads=True) as cur:
            self._object_payloads[obj.oid] = obj.payload
            cur.execute(
                """
                INSERT INTO objects (
                    oid, namespace, name, type, schema_version, payload_json, metadata_json,
                    provenance_json, version, immutable, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    obj.oid,
                    obj.namespace,
                    obj.name,
                    obj.type.value,
                    obj.schema_version,
                    dumps(self._memory_payload_marker(present=True)),
                    dumps(obj.metadata),
                    dumps(obj.provenance),
                    obj.version,
                    int(obj.immutable),
                    obj.created_by,
                    obj.created_at,
                    obj.updated_at,
                ),
            )

    def update_object(self, obj: AgentObject) -> None:
        with self.transaction(include_object_payloads=True) as cur:
            self._object_payloads[obj.oid] = obj.payload
            cur.execute(
                """
                UPDATE objects
                   SET namespace = ?, name = ?, type = ?, schema_version = ?, payload_json = ?, metadata_json = ?,
                       provenance_json = ?, version = ?, immutable = ?, created_by = ?,
                       created_at = ?, updated_at = ?
                 WHERE oid = ?
                """,
                (
                    obj.namespace,
                    obj.name,
                    obj.type.value,
                    obj.schema_version,
                    dumps(self._memory_payload_marker(present=True)),
                    dumps(obj.metadata),
                    dumps(obj.provenance),
                    obj.version,
                    int(obj.immutable),
                    obj.created_by,
                    obj.created_at,
                    obj.updated_at,
                    obj.oid,
                ),
            )

    def get_object(self, oid: str) -> AgentObject | None:
        rows = self._query("SELECT * FROM objects WHERE oid = ?", (oid,))
        # A row without an in-memory payload is a directory remnant from a prior
        # runtime instance or checkpoint restore, not a materializable Object.
        if not rows or oid not in self._object_payloads:
            return None
        return self._row_to_object(rows[0])

    def get_object_by_name(self, name: str, namespace: str) -> AgentObject | None:
        rows = self._query("SELECT * FROM objects WHERE namespace = ? AND name = ?", (namespace, name))
        if not rows or rows[0]["oid"] not in self._object_payloads:
            return None
        return self._row_to_object(rows[0])

    def object_name_exists(self, name: str, namespace: str, except_oid: str | None = None) -> bool:
        rows = self._query("SELECT oid FROM objects WHERE namespace = ? AND name = ?", (namespace, name))
        return any(row["oid"] != except_oid for row in rows)

    def list_objects(self, namespace: str | None = None) -> list[AgentObject]:
        if namespace is None:
            rows = self._query("SELECT * FROM objects ORDER BY updated_at DESC, created_at DESC, oid ASC")
        else:
            rows = self._query(
                "SELECT * FROM objects WHERE namespace = ? ORDER BY updated_at DESC, created_at DESC, oid ASC",
                (namespace,),
            )
        return [
            self._row_to_object(row)
            for row in rows
            if row["oid"] in self._object_payloads
        ]

    def list_object_oids_created_by(self, created_by: str) -> list[str]:
        rows = self._query("SELECT oid FROM objects WHERE created_by = ? ORDER BY created_at", (created_by,))
        return [str(row["oid"]) for row in rows]

    def list_objects_created_by(self, created_by: str) -> list[AgentObject]:
        rows = self._query("SELECT * FROM objects WHERE created_by = ? ORDER BY created_at, oid", (created_by,))
        return [
            self._row_to_object(row)
            for row in rows
            if row["oid"] in self._object_payloads
        ]

    def delete_object(self, oid: str) -> None:
        with self.transaction(include_object_payloads=True) as cur:
            self._object_payloads.pop(oid, None)
            cur.execute("DELETE FROM object_links WHERE src_oid = ? OR dst_oid = ?", (oid, oid))
            cur.execute("DELETE FROM objects WHERE oid = ?", (oid,))

    def insert_namespace(self, namespace: ObjectNamespace) -> None:
        self._execute(
            """
            INSERT INTO object_namespaces (
                namespace, parent_namespace, metadata_json, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                namespace.namespace,
                namespace.parent_namespace,
                dumps(namespace.metadata),
                namespace.created_by,
                namespace.created_at,
                namespace.updated_at,
            ),
        )

    def get_namespace(self, namespace: str) -> ObjectNamespace | None:
        rows = self._query("SELECT * FROM object_namespaces WHERE namespace = ?", (namespace,))
        return self._row_to_namespace(rows[0]) if rows else None

    def namespace_exists(self, namespace: str) -> bool:
        rows = self._query("SELECT 1 FROM object_namespaces WHERE namespace = ?", (namespace,))
        return bool(rows)

    def list_namespaces(self, parent_namespace: str | None = None) -> list[ObjectNamespace]:
        if parent_namespace is None:
            rows = self._query("SELECT * FROM object_namespaces ORDER BY namespace")
        else:
            rows = self._query(
                "SELECT * FROM object_namespaces WHERE parent_namespace = ? ORDER BY namespace",
                (parent_namespace,),
            )
        return [self._row_to_namespace(row) for row in rows]

    def list_namespaces_created_by(self, created_by: str) -> list[ObjectNamespace]:
        rows = self._query(
            "SELECT * FROM object_namespaces WHERE created_by = ? ORDER BY namespace",
            (created_by,),
        )
        return [self._row_to_namespace(row) for row in rows]

    def insert_link(self, link: ObjectLink) -> None:
        self._execute(
            "INSERT INTO object_links VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                link.link_id,
                link.src,
                link.relation.value,
                link.dst,
                dumps(link.metadata),
                link.created_by,
                link.created_at,
            ),
        )

    def list_links(self, src: str | None = None, dst: str | None = None) -> list[ObjectLink]:
        if src is not None:
            rows = self._query("SELECT * FROM object_links WHERE src_oid = ?", (src,))
        elif dst is not None:
            rows = self._query("SELECT * FROM object_links WHERE dst_oid = ?", (dst,))
        else:
            rows = self._query("SELECT * FROM object_links")
        return [self._row_to_link(row) for row in rows]

    def insert_process(self, process: AgentProcess) -> None:
        self._execute(
            """
            INSERT INTO processes (
                pid, parent_pid, image_id, status, goal_oid, memory_view_json,
                capabilities_json, loaded_skills_json, tool_table_json, event_cursor,
                checkpoint_head, status_message, resource_budget_json, resource_usage_json,
                working_directory, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._process_params(process),
        )

    def update_process(self, process: AgentProcess) -> None:
        self._execute(
            """
            UPDATE processes
               SET parent_pid = ?, image_id = ?, status = ?, goal_oid = ?,
                   memory_view_json = ?, capabilities_json = ?, loaded_skills_json = ?,
                   tool_table_json = ?, event_cursor = ?, checkpoint_head = ?,
                   status_message = ?, resource_budget_json = ?, resource_usage_json = ?,
                   working_directory = ?, created_at = ?, updated_at = ?
             WHERE pid = ?
            """,
            (
                process.parent_pid,
                process.image_id,
                process.status.value,
                process.goal_oid,
                dumps(process.memory_view) if process.memory_view else None,
                dumps(process.capabilities),
                dumps(process.loaded_skills),
                dumps(process.tool_table),
                process.event_cursor,
                process.checkpoint_head,
                process.status_message,
                dumps(process.resource_budget),
                dumps(process.resource_usage),
                process.working_directory,
                process.created_at,
                process.updated_at,
                process.pid,
            ),
        )

    def get_process(self, pid: str) -> AgentProcess | None:
        rows = self._query("SELECT * FROM processes WHERE pid = ?", (pid,))
        return self._row_to_process(rows[0]) if rows else None

    def list_processes(self) -> list[AgentProcess]:
        return [self._row_to_process(row) for row in self._query("SELECT * FROM processes")]

    def list_processes_by_status(self, status: ProcessStatus | str) -> list[AgentProcess]:
        selected = ProcessStatus(status).value
        rows = self._query(
            "SELECT * FROM processes WHERE status = ? ORDER BY created_at, pid",
            (selected,),
        )
        return [self._row_to_process(row) for row in rows]

    def list_child_processes(self, parent_pid: str) -> list[AgentProcess]:
        rows = self._query(
            "SELECT * FROM processes WHERE parent_pid = ? ORDER BY created_at, pid",
            (parent_pid,),
        )
        return [self._row_to_process(row) for row in rows]

    def upsert_resource_reservation(self, reservation: ResourceReservation) -> None:
        self._execute(
            """
            INSERT INTO process_resource_reservations (
                parent_pid, child_pid, reservation_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(parent_pid, child_pid) DO UPDATE SET
                reservation_json = excluded.reservation_json,
                updated_at = excluded.updated_at
            """,
            (
                reservation.parent_pid,
                reservation.child_pid,
                dumps(reservation.reserved),
                reservation.created_at,
                reservation.updated_at,
            ),
        )

    def get_resource_reservation(self, parent_pid: str, child_pid: str) -> ResourceReservation | None:
        rows = self._query(
            "SELECT * FROM process_resource_reservations WHERE parent_pid = ? AND child_pid = ?",
            (parent_pid, child_pid),
        )
        return self._row_to_resource_reservation(rows[0]) if rows else None

    def list_resource_reservations(
        self,
        *,
        parent_pid: str | None = None,
        child_pid: str | None = None,
    ) -> list[ResourceReservation]:
        clauses: list[str] = []
        params: list[Any] = []
        if parent_pid is not None:
            clauses.append("parent_pid = ?")
            params.append(parent_pid)
        if child_pid is not None:
            clauses.append("child_pid = ?")
            params.append(child_pid)
        sql = "SELECT * FROM process_resource_reservations"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY parent_pid, child_pid"
        return [self._row_to_resource_reservation(row) for row in self._query(sql, params)]

    def delete_resource_reservation(self, parent_pid: str, child_pid: str) -> None:
        self._execute(
            "DELETE FROM process_resource_reservations WHERE parent_pid = ? AND child_pid = ?",
            (parent_pid, child_pid),
        )

    def delete_resource_reservations_for_process(self, pid: str) -> None:
        self._execute(
            "DELETE FROM process_resource_reservations WHERE parent_pid = ? OR child_pid = ?",
            (pid, pid),
        )

    def select_table_rows(
        self,
        table: str,
        where_sql: str = "",
        params: Iterable[Any] = (),
        *,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {table}"
        if where_sql:
            sql += f" WHERE {where_sql}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        return [self._row_to_dict(row) for row in self._query(sql, params)]

    def insert_table_row(self, table: str, row: dict[str, Any]) -> None:
        columns = list(row)
        placeholders = ", ".join("?" for _ in columns)
        col_sql = ", ".join(columns)
        self._execute(
            f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
            tuple(row[column] for column in columns),
        )

    def delete_table_rows(self, table: str, where_sql: str, params: Iterable[Any] = ()) -> None:
        self._execute(f"DELETE FROM {table} WHERE {where_sql}", params)

    def object_payload(self, oid: str) -> Any:
        return self._object_payloads[oid]

    def set_object_payload(self, oid: str, payload: Any) -> None:
        self._object_payloads[oid] = payload

    def forget_object_payload(self, oid: str) -> None:
        self._object_payloads.pop(oid, None)

    def insert_event(self, event: Event) -> None:
        self._execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.type.value,
                event.source,
                event.target,
                dumps(event.payload),
                event.priority.value,
                event.created_at,
                event.correlation_id,
                dumps(event.causality),
            ),
        )

    def list_events(self, target: str | None = None) -> list[Event]:
        if target is None:
            rows = self._query("SELECT * FROM events ORDER BY created_at")
        else:
            rows = self._query(
                "SELECT * FROM events WHERE target IS NULL OR target = ? ORDER BY created_at",
                (target,),
            )
        return [self._row_to_event(row) for row in rows]

    def insert_capability(self, cap: Capability) -> None:
        self._execute(
            """
            INSERT INTO capabilities (
                cap_id, subject, resource, rights_json, constraints_json,
                issued_by, issued_at, expires_at, delegable, revocable, effect,
                issuer_cap_id, parent_cap_id, delegation_depth, max_delegation_depth,
                uses_remaining, status, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cap.cap_id,
                cap.subject,
                cap.resource,
                dumps(cap.rights),
                dumps(cap.constraints),
                cap.issued_by,
                cap.issued_at,
                cap.expires_at,
                int(cap.delegable),
                int(cap.revocable),
                cap.effect.value,
                cap.issuer_cap_id,
                cap.parent_cap_id,
                cap.delegation_depth,
                cap.max_delegation_depth,
                cap.uses_remaining,
                cap.status.value,
                dumps(cap.metadata),
            ),
        )

    def update_capability(self, cap: Capability) -> None:
        self._execute(
            """
            UPDATE capabilities
               SET subject = ?, resource = ?, rights_json = ?, constraints_json = ?,
                   issued_by = ?, issued_at = ?, expires_at = ?, delegable = ?,
                   revocable = ?, effect = ?, issuer_cap_id = ?, parent_cap_id = ?,
                   delegation_depth = ?, max_delegation_depth = ?, uses_remaining = ?,
                   status = ?, metadata_json = ?
             WHERE cap_id = ?
            """,
            (
                cap.subject,
                cap.resource,
                dumps(cap.rights),
                dumps(cap.constraints),
                cap.issued_by,
                cap.issued_at,
                cap.expires_at,
                int(cap.delegable),
                int(cap.revocable),
                cap.effect.value,
                cap.issuer_cap_id,
                cap.parent_cap_id,
                cap.delegation_depth,
                cap.max_delegation_depth,
                cap.uses_remaining,
                cap.status.value,
                dumps(cap.metadata),
                cap.cap_id,
            ),
        )

    def get_capability(self, cap_id: str) -> Capability | None:
        rows = self._query("SELECT * FROM capabilities WHERE cap_id = ?", (cap_id,))
        return self._row_to_capability(rows[0]) if rows else None

    def list_capabilities(self, subject: str | None = None) -> list[Capability]:
        if subject is None:
            rows = self._query("SELECT * FROM capabilities")
        else:
            rows = self._query("SELECT * FROM capabilities WHERE subject = ?", (subject,))
        return [self._row_to_capability(row) for row in rows]

    def insert_audit(self, record: AuditRecord) -> None:
        self._execute(
            "INSERT INTO audit_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.record_id,
                record.timestamp,
                record.actor,
                record.action,
                record.target,
                dumps(record.input_refs),
                dumps(record.output_refs),
                dumps(record.capability_refs),
                dumps(record.decision) if record.decision is not None else None,
                record.correlation_id,
                record.parent_record_id,
            ),
        )

    def list_audit(
        self,
        limit: int | None = None,
        *,
        actor: str | None = None,
        target: str | None = None,
        match_any: bool = False,
    ) -> list[AuditRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if actor is not None:
            clauses.append("actor = ?")
            params.append(actor)
        if target is not None:
            clauses.append("target = ?")
            params.append(target)
        joiner = " OR " if match_any and len(clauses) > 1 else " AND "
        where = f" WHERE {joiner.join(clauses)}" if clauses else ""
        order = "ORDER BY timestamp, rowid"
        if limit is None:
            return [self._row_to_audit(row) for row in self._query(f"SELECT * FROM audit_records{where} {order}", params)]
        selected_limit = int(limit)
        if selected_limit <= 0:
            return []
        # Limited audit reads are used by the GUI and API list views. Select the
        # newest window first, then return it chronologically so append streams
        # do not lose recent records once the table is larger than the window.
        limited = (
            f"SELECT audit_records.*, rowid AS _audit_rowid FROM audit_records{where} "
            "ORDER BY timestamp DESC, rowid DESC LIMIT ?"
        )
        rows = self._query(f"SELECT * FROM ({limited}) ORDER BY timestamp, _audit_rowid", [*params, selected_limit])
        return [self._row_to_audit(row) for row in rows]

    def insert_external_effect(self, record: ExternalEffectRecord) -> None:
        self._execute(
            """
            INSERT INTO external_effects (
                effect_id, record_id, event_id, pid, provider, operation, target,
                rollback_class, rollback_status, state_mutation, information_flow,
                provider_metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.effect_id,
                record.record_id,
                record.event_id,
                record.pid,
                record.provider,
                record.operation,
                record.target,
                record.rollback_class.value,
                record.rollback_status.value,
                int(record.state_mutation),
                int(record.information_flow),
                dumps(record.provider_metadata),
                record.created_at,
            ),
        )

    def list_external_effects(
        self,
        *,
        created_after: str | None = None,
        pid: str | None = None,
        pids: Iterable[str] | None = None,
    ) -> list[ExternalEffectRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if created_after is not None:
            clauses.append("created_at > ?")
            params.append(created_after)
        if pid is not None:
            clauses.append("pid = ?")
            params.append(pid)
        if pids is not None:
            selected_pids = list(dict.fromkeys(str(item) for item in pids))
            if not selected_pids:
                return []
            placeholders = ", ".join("?" for _ in selected_pids)
            clauses.append(f"pid IN ({placeholders})")
            params.extend(selected_pids)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(
            f"SELECT * FROM external_effects{where} ORDER BY created_at, effect_id",
            params,
        )
        return [self._row_to_external_effect(row) for row in rows]

    def insert_human_request(self, request: HumanRequest) -> None:
        self._execute(
            "INSERT INTO human_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.request_id,
                request.pid,
                request.human,
                dumps(request.payload),
                request.status.value,
                dumps(request.decision) if request.decision is not None else None,
                int(request.blocking),
                request.created_at,
                request.updated_at,
            ),
        )

    def update_human_request(self, request: HumanRequest) -> None:
        self._execute(
            """
            UPDATE human_requests
               SET pid = ?, human = ?, payload_json = ?, status = ?, decision_json = ?,
                   blocking = ?, created_at = ?, updated_at = ?
             WHERE request_id = ?
            """,
            (
                request.pid,
                request.human,
                dumps(request.payload),
                request.status.value,
                dumps(request.decision) if request.decision is not None else None,
                int(request.blocking),
                request.created_at,
                request.updated_at,
                request.request_id,
            ),
        )

    def get_human_request(self, request_id: str) -> HumanRequest | None:
        rows = self._query("SELECT * FROM human_requests WHERE request_id = ?", (request_id,))
        return self._row_to_human_request(rows[0]) if rows else None

    def list_human_requests(self, pid: str | None = None) -> list[HumanRequest]:
        if pid is None:
            rows = self._query("SELECT * FROM human_requests ORDER BY created_at")
        else:
            rows = self._query(
                "SELECT * FROM human_requests WHERE pid = ? ORDER BY created_at",
                (pid,),
            )
        return [self._row_to_human_request(row) for row in rows]

    def insert_llm_call(self, record: LLMCallRecord) -> None:
        self._execute(
            """
            INSERT INTO llm_calls (
                call_id, pid, image_id, purpose, status, api, model, request_id, response_id,
                messages_json, tools_json, request_options_json, response_content, tool_calls_json,
                reasoning_json, usage_json, raw_response_json, observability_json, error, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.call_id,
                record.pid,
                record.image_id,
                record.purpose,
                record.status,
                record.api,
                record.model,
                record.request_id,
                record.response_id,
                dumps(record.messages),
                dumps(record.tools),
                dumps(record.request_options),
                record.response_content,
                dumps(record.tool_calls),
                dumps(record.reasoning) if record.reasoning is not None else None,
                dumps(record.usage),
                dumps(record.raw_response) if record.raw_response is not None else None,
                dumps(record.observability),
                record.error,
                record.created_at,
                record.completed_at,
            ),
        )

    def list_llm_calls(self, pid: str | None = None, limit: int | None = None) -> list[LLMCallRecord]:
        selected_limit = self._llm_call_limit(limit)
        params: list[Any] = []
        sql = "SELECT * FROM llm_calls"
        if pid is not None:
            sql += " WHERE pid = ?"
            params.append(pid)
        sql += " ORDER BY created_at, call_id"
        sql += " LIMIT ?"
        params.append(selected_limit)
        return [self._row_to_llm_call(row) for row in self._query(sql, params)]

    def upsert_llm_pending_action(self, pid: str, pending: dict[str, Any]) -> None:
        now = utc_now()
        created_at = str(pending.get("created_at") or now)
        self._execute(
            """
            INSERT INTO llm_pending_actions (
                pid, wait_type, request_id, child_pid, filters_json, action_json,
                content_preview, tool_call_count, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pid) DO UPDATE SET
                wait_type = excluded.wait_type,
                request_id = excluded.request_id,
                child_pid = excluded.child_pid,
                filters_json = excluded.filters_json,
                action_json = excluded.action_json,
                content_preview = excluded.content_preview,
                tool_call_count = excluded.tool_call_count,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                pid,
                str(pending["wait_type"]),
                pending.get("request_id"),
                pending.get("child_pid"),
                dumps(pending.get("filters") or {}),
                dumps(pending.get("action") or {}),
                str(pending.get("content_preview") or ""),
                int(pending.get("tool_call_count") or 0),
                str(pending.get("status") or "pending"),
                created_at,
                now,
            ),
        )

    def get_llm_pending_action(self, pid: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM llm_pending_actions WHERE pid = ?", (pid,))
        return self._row_to_llm_pending_action(rows[0]) if rows else None

    def list_llm_pending_actions(self, *, status: str | None = "pending") -> list[dict[str, Any]]:
        if status is None:
            rows = self._query("SELECT * FROM llm_pending_actions ORDER BY updated_at, pid")
        else:
            rows = self._query("SELECT * FROM llm_pending_actions WHERE status = ? ORDER BY updated_at, pid", (status,))
        return [self._row_to_llm_pending_action(row) for row in rows]

    def complete_llm_pending_action(self, pid: str) -> None:
        self._execute(
            "UPDATE llm_pending_actions SET status = ?, updated_at = ? WHERE pid = ?",
            ("completed", utc_now(), pid),
        )

    def insert_process_message(self, message: ProcessMessage) -> None:
        self._execute(
            """
            INSERT INTO process_messages (
                message_id, sender, recipient_pid, kind, channel, correlation_id, reply_to,
                subject, body, payload_json, status, created_at, updated_at, acked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.message_id,
                message.sender,
                message.recipient_pid,
                message.kind.value,
                message.channel,
                message.correlation_id,
                message.reply_to,
                message.subject,
                message.body,
                dumps(message.payload),
                message.status.value,
                message.created_at,
                message.updated_at,
                message.acked_at,
            ),
        )

    def update_process_message(self, message: ProcessMessage) -> None:
        self._execute(
            """
            UPDATE process_messages
               SET sender = ?, recipient_pid = ?, kind = ?, subject = ?, body = ?,
                   channel = ?, correlation_id = ?, reply_to = ?, payload_json = ?,
                   status = ?, created_at = ?, updated_at = ?, acked_at = ?
             WHERE message_id = ?
            """,
            (
                message.sender,
                message.recipient_pid,
                message.kind.value,
                message.subject,
                message.body,
                message.channel,
                message.correlation_id,
                message.reply_to,
                dumps(message.payload),
                message.status.value,
                message.created_at,
                message.updated_at,
                message.acked_at,
                message.message_id,
            ),
        )

    def get_process_message(self, message_id: str) -> ProcessMessage | None:
        rows = self._query("SELECT * FROM process_messages WHERE message_id = ?", (message_id,))
        return self._row_to_process_message(rows[0]) if rows else None

    def list_process_messages(
        self,
        recipient_pid: str | None = None,
        *,
        status: ProcessMessageStatus | str | None = None,
        kind: ProcessMessageKind | str | None = None,
        sender: str | None = None,
        channel: str | None = None,
        correlation_id: str | None = None,
        reply_to: str | None = None,
        message_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[ProcessMessage]:
        clauses: list[str] = []
        params: list[Any] = []
        if message_ids is not None and not message_ids:
            return []
        if recipient_pid is not None:
            clauses.append("recipient_pid = ?")
            params.append(recipient_pid)
        if status is not None:
            selected_status = ProcessMessageStatus(status)
            clauses.append("status = ?")
            params.append(selected_status.value)
        if kind is not None:
            selected_kind = ProcessMessageKind(kind)
            clauses.append("kind = ?")
            params.append(selected_kind.value)
        if sender is not None:
            clauses.append("sender = ?")
            params.append(sender)
        if channel is not None:
            clauses.append("channel = ?")
            params.append(channel)
        if correlation_id is not None:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        if reply_to is not None:
            clauses.append("reply_to = ?")
            params.append(reply_to)
        if message_ids is not None:
            placeholders = ", ".join("?" for _ in message_ids)
            clauses.append(f"message_id IN ({placeholders})")
            params.extend(message_ids)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = self._query(f"SELECT * FROM process_messages{where} ORDER BY created_at, message_id{limit_sql}", params)
        return [self._row_to_process_message(row) for row in rows]

    def insert_object_task(self, task: ObjectTask) -> None:
        self._execute(
            """
            INSERT INTO object_tasks (
                task_id, owner_oid, creator_pid, runner_pid, tool, tool_id, status,
                notification_json, owner_watch_json, result_oid, error, wait_json, created_at,
                updated_at, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._object_task_params(task),
        )

    def update_object_task(self, task: ObjectTask) -> None:
        self._execute(
            """
            UPDATE object_tasks
                   SET owner_oid = ?, creator_pid = ?, runner_pid = ?, tool = ?, tool_id = ?,
                   status = ?, notification_json = ?, owner_watch_json = ?, result_oid = ?, error = ?, wait_json = ?,
                   created_at = ?, updated_at = ?, started_at = ?, completed_at = ?
             WHERE task_id = ?
            """,
            (
                task.owner_oid,
                task.creator_pid,
                task.runner_pid,
                task.tool,
                task.tool_id,
                task.status.value,
                dumps(task.notification),
                dumps(task.owner_watch),
                task.result_oid,
                task.error,
                dumps(task.wait),
                task.created_at,
                task.updated_at,
                task.started_at,
                task.completed_at,
                task.task_id,
            ),
        )

    def get_object_task(self, task_id: str) -> ObjectTask | None:
        rows = self._query("SELECT * FROM object_tasks WHERE task_id = ?", (task_id,))
        return self._row_to_object_task(rows[0]) if rows else None

    def list_object_tasks(
        self,
        *,
        owner_oid: str | None = None,
        creator_pid: str | None = None,
        statuses: Iterable[str | ObjectTaskStatus] | None = None,
        include_terminal: bool = True,
        limit: int | None = None,
    ) -> list[ObjectTask]:
        clauses: list[str] = []
        params: list[Any] = []
        if owner_oid is not None:
            clauses.append("owner_oid = ?")
            params.append(owner_oid)
        if creator_pid is not None:
            clauses.append("creator_pid = ?")
            params.append(creator_pid)
        if statuses is not None:
            selected = [ObjectTaskStatus(status).value for status in statuses]
            if not selected:
                return []
            clauses.append(f"status IN ({', '.join('?' for _ in selected)})")
            params.extend(selected)
        elif not include_terminal:
            terminal = [
                ObjectTaskStatus.SUCCEEDED.value,
                ObjectTaskStatus.FAILED.value,
                ObjectTaskStatus.CANCELLED.value,
                ObjectTaskStatus.ABANDONED.value,
            ]
            clauses.append(f"status NOT IN ({', '.join('?' for _ in terminal)})")
            params.extend(terminal)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(max(0, int(limit)))
        rows = self._query(f"SELECT * FROM object_tasks{where} ORDER BY updated_at DESC, created_at DESC, task_id ASC{limit_sql}", params)
        return [self._row_to_object_task(row) for row in rows]

    def mark_object_tasks_abandoned(self, reason: str) -> list[str]:
        active = self.list_object_tasks(include_terminal=False)
        if not active:
            return []
        now = utc_now()
        task_ids = [task.task_id for task in active]
        with self._lock:
            self.conn.executemany(
                """
                UPDATE object_tasks
                   SET status = ?, error = ?, updated_at = ?, completed_at = ?
                 WHERE task_id = ?
                """,
                [(ObjectTaskStatus.ABANDONED.value, reason, now, now, task_id) for task_id in task_ids],
            )
            self.conn.commit()
        return task_ids

    def insert_tool(self, handle: ToolHandle, spec: ToolSpec, registered_by: str, created_at: str, ephemeral: bool) -> None:
        self._execute(
            "INSERT INTO tools VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                handle.tool_id,
                handle.name,
                dumps(spec),
                handle.scope,
                registered_by,
                created_at,
                int(ephemeral),
            ),
        )

    def update_tool(self, handle: ToolHandle, spec: ToolSpec, registered_by: str, ephemeral: bool) -> None:
        self._execute(
            """
            UPDATE tools
               SET name = ?, spec_json = ?, scope = ?, registered_by = ?, ephemeral = ?
             WHERE tool_id = ?
            """,
            (
                handle.name,
                dumps(spec),
                handle.scope,
                registered_by,
                int(ephemeral),
                handle.tool_id,
            ),
        )

    def get_tool_spec(self, tool_id: str) -> ToolSpec | None:
        rows = self._query("SELECT * FROM tools WHERE tool_id = ?", (tool_id,))
        if not rows:
            return None
        return self._dict_to_tool_spec(loads(rows[0]["spec_json"]))

    def list_tools(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._query("SELECT * FROM tools ORDER BY created_at")]

    def insert_tool_candidate(self, candidate: ToolCandidate) -> None:
        self._execute(
            """
            INSERT INTO tool_candidates (
                candidate_id, pid, spec_json, source_code, tests_json,
                requested_capabilities_json, status, registered_tool_id,
                validation_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.candidate_id,
                candidate.pid,
                dumps(candidate.spec),
                candidate.source_code,
                dumps(candidate.tests),
                dumps(candidate.requested_capabilities),
                candidate.status.value,
                candidate.registered_tool_id,
                dumps(candidate.validation) if candidate.validation is not None else None,
                candidate.created_at,
                candidate.updated_at,
            ),
        )

    def update_tool_candidate(self, candidate: ToolCandidate) -> None:
        self._execute(
            """
            UPDATE tool_candidates
               SET pid = ?, spec_json = ?, source_code = ?, tests_json = ?,
                   requested_capabilities_json = ?, status = ?, registered_tool_id = ?,
                   validation_json = ?, created_at = ?, updated_at = ?
             WHERE candidate_id = ?
            """,
            (
                candidate.pid,
                dumps(candidate.spec),
                candidate.source_code,
                dumps(candidate.tests),
                dumps(candidate.requested_capabilities),
                candidate.status.value,
                candidate.registered_tool_id,
                dumps(candidate.validation) if candidate.validation is not None else None,
                candidate.created_at,
                candidate.updated_at,
                candidate.candidate_id,
            ),
        )

    def get_tool_candidate(self, candidate_id: str) -> ToolCandidate | None:
        rows = self._query("SELECT * FROM tool_candidates WHERE candidate_id = ?", (candidate_id,))
        return self._row_to_tool_candidate(rows[0]) if rows else None

    def upsert_skill(
        self,
        skill: SkillPackage,
        *,
        source_type: str,
        source: str | None,
        package_sha256: str,
        registered_by: str,
        created_at: str,
    ) -> None:
        self._execute(
            """
            INSERT INTO skills (
                skill_id, name, version, package_json, source_type, source,
                package_sha256, registered_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(skill_id) DO UPDATE SET
                name = excluded.name,
                version = excluded.version,
                package_json = excluded.package_json,
                source_type = excluded.source_type,
                source = excluded.source,
                package_sha256 = excluded.package_sha256,
                registered_by = excluded.registered_by,
                updated_at = excluded.updated_at
            """,
            (
                skill.skill_id,
                skill.name,
                skill.version,
                dumps(skill),
                source_type,
                source,
                package_sha256,
                registered_by,
                created_at,
                created_at,
            ),
        )

    def get_skill(self, skill_id: str) -> tuple[SkillPackage, dict[str, Any]] | None:
        rows = self._query("SELECT * FROM skills WHERE skill_id = ?", (skill_id,))
        if not rows:
            return None
        row = rows[0]
        return self._dict_to_skill_package(loads(row["package_json"], {})), self._skill_row_metadata(row)

    def list_skills(self, text: str | None = None, limit: int | None = None) -> list[tuple[SkillPackage, dict[str, Any]]]:
        params: list[Any] = []
        sql = "SELECT * FROM skills"
        if text:
            needle = f"%{text.lower()}%"
            sql += " WHERE lower(skill_id) LIKE ? OR lower(name) LIKE ? OR lower(package_json) LIKE ?"
            params.extend([needle, needle, needle])
        sql += " ORDER BY name, skill_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [
            (self._dict_to_skill_package(loads(row["package_json"], {})), self._skill_row_metadata(row))
            for row in self._query(sql, params)
        ]

    def insert_skill_trust(
        self,
        *,
        trust_id: str,
        source_type: str,
        source: str,
        package_sha256: str,
        trusted_by: str,
        created_at: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._execute(
            """
            INSERT OR REPLACE INTO skill_trust (
                trust_id, source_type, source, package_sha256, trusted_by, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trust_id,
                source_type,
                source,
                package_sha256,
                trusted_by,
                created_at,
                dumps(metadata or {}),
            ),
        )

    def delete_skill_trust(self, *, source_type: str, source: str, package_sha256: str) -> None:
        self._execute(
            "DELETE FROM skill_trust WHERE source_type = ? AND source = ? AND package_sha256 = ?",
            (source_type, source, package_sha256),
        )

    def is_skill_trusted(self, *, source_type: str, source: str, package_sha256: str) -> bool:
        rows = self._query(
            "SELECT 1 FROM skill_trust WHERE source_type = ? AND source = ? AND package_sha256 = ?",
            (source_type, source, package_sha256),
        )
        return bool(rows)

    def list_skill_trust(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._query("SELECT * FROM skill_trust ORDER BY created_at, source")]

    def upsert_jsonrpc_endpoint(self, endpoint: JsonRpcEndpointSpec, *, registered_by: str, created_at: str) -> None:
        self._execute(
            """
            INSERT INTO jsonrpc_endpoints (
                endpoint_id, spec_json, registered_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(endpoint_id) DO UPDATE SET
                spec_json = excluded.spec_json,
                registered_by = excluded.registered_by,
                updated_at = excluded.updated_at
            """,
            (
                endpoint.endpoint_id,
                dumps(endpoint),
                registered_by,
                created_at,
                created_at,
            ),
        )

    def get_jsonrpc_endpoint(self, endpoint_id: str) -> tuple[JsonRpcEndpointSpec, dict[str, Any]] | None:
        rows = self._query("SELECT * FROM jsonrpc_endpoints WHERE endpoint_id = ?", (endpoint_id,))
        if not rows:
            return None
        row = rows[0]
        return self._dict_to_jsonrpc_endpoint(loads(row["spec_json"], {})), self._jsonrpc_endpoint_row_metadata(row)

    def list_jsonrpc_endpoints(self, text: str | None = None, limit: int | None = None) -> list[tuple[JsonRpcEndpointSpec, dict[str, Any]]]:
        params: list[Any] = []
        sql = "SELECT * FROM jsonrpc_endpoints"
        if text:
            needle = f"%{text.lower()}%"
            sql += " WHERE lower(endpoint_id) LIKE ? OR lower(spec_json) LIKE ?"
            params.extend([needle, needle])
        sql += " ORDER BY endpoint_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [
            (self._dict_to_jsonrpc_endpoint(loads(row["spec_json"], {})), self._jsonrpc_endpoint_row_metadata(row))
            for row in self._query(sql, params)
        ]

    def delete_jsonrpc_endpoint(self, endpoint_id: str) -> None:
        self._execute("DELETE FROM jsonrpc_endpoints WHERE endpoint_id = ?", (endpoint_id,))

    def upsert_image(
        self,
        image: AgentImage,
        *,
        registered_by: str,
        source: str | None,
        created_at: str,
    ) -> None:
        self._execute(
            """
            INSERT INTO images (
                image_id, manifest_json, registered_by, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(image_id) DO UPDATE SET
                manifest_json = excluded.manifest_json,
                registered_by = excluded.registered_by,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                image.image_id,
                dumps(image),
                registered_by,
                source,
                created_at,
                created_at,
            ),
        )

    def get_image(self, image_id: str) -> tuple[AgentImage, dict[str, Any]] | None:
        rows = self._query("SELECT * FROM images WHERE image_id = ?", (image_id,))
        if not rows:
            return None
        row = rows[0]
        return self._dict_to_agent_image(loads(row["manifest_json"], {})), self._image_row_metadata(row)

    def list_images(self) -> list[tuple[AgentImage, dict[str, Any]]]:
        return [
            (self._dict_to_agent_image(loads(row["manifest_json"], {})), self._image_row_metadata(row))
            for row in self._query("SELECT * FROM images ORDER BY image_id")
        ]

    def insert_image_artifact(
        self,
        *,
        artifact_id: str,
        kind: str,
        artifact: dict[str, Any],
        sha256: str,
        created_by: str,
        created_at: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._execute(
            """
            INSERT INTO image_artifacts (
                artifact_id, kind, artifact_json, sha256, created_by, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                kind,
                dumps(artifact),
                sha256,
                created_by,
                created_at,
                dumps(metadata or {}),
            ),
        )

    def get_image_artifact(self, artifact_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        rows = self._query("SELECT * FROM image_artifacts WHERE artifact_id = ?", (artifact_id,))
        if not rows:
            return None
        row = rows[0]
        return loads(row["artifact_json"], {}), {
            "artifact_id": row["artifact_id"],
            "kind": row["kind"],
            "sha256": row["sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "metadata": loads(row["metadata_json"], {}),
        }

    def list_image_artifacts(self) -> list[dict[str, Any]]:
        return [
            {
                "artifact_id": row["artifact_id"],
                "kind": row["kind"],
                "sha256": row["sha256"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "metadata": loads(row["metadata_json"], {}),
            }
            for row in self._query("SELECT * FROM image_artifacts ORDER BY created_at, artifact_id")
        ]

    def upsert_runtime_module(
        self,
        *,
        module_id: str,
        name: str,
        version: str,
        entrypoint: str,
        manifest_path: str,
        manifest_sha256: str,
        source_path: str,
        source_sha256: str,
        status: str,
        loaded_at: str | None,
        registered: dict[str, Any],
        error: str | None,
        metadata: dict[str, Any],
    ) -> None:
        updated_at = utc_now()
        self._execute(
            """
            INSERT INTO runtime_modules (
                module_id, name, version, entrypoint, manifest_path,
                manifest_sha256, source_path, source_sha256, status, loaded_at,
                registered_json, error, metadata_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(module_id) DO UPDATE SET
                name = excluded.name,
                version = excluded.version,
                entrypoint = excluded.entrypoint,
                manifest_path = excluded.manifest_path,
                manifest_sha256 = excluded.manifest_sha256,
                source_path = excluded.source_path,
                source_sha256 = excluded.source_sha256,
                status = excluded.status,
                loaded_at = excluded.loaded_at,
                registered_json = excluded.registered_json,
                error = excluded.error,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                module_id,
                name,
                version,
                entrypoint,
                manifest_path,
                manifest_sha256,
                source_path,
                source_sha256,
                status,
                loaded_at,
                dumps(registered),
                error,
                dumps(metadata),
                updated_at,
            ),
        )

    def get_runtime_module(self, module_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM runtime_modules WHERE module_id = ?", (module_id,))
        return self._runtime_module_row(rows[0]) if rows else None

    def list_runtime_modules(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runtime_modules ORDER BY module_id"
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._runtime_module_row(row) for row in self._query(sql, params)]

    def insert_checkpoint(self, checkpoint: Checkpoint, snapshot: dict[str, Any]) -> None:
        self._execute(
            """
            INSERT INTO checkpoints (
                checkpoint_id, pid, reason, snapshot_json, created_at,
                created_by, snapshot_version, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.checkpoint_id,
                checkpoint.pid,
                checkpoint.reason,
                dumps(snapshot),
                checkpoint.created_at,
                checkpoint.created_by,
                checkpoint.snapshot_version,
                dumps(checkpoint.metadata or {}),
            ),
        )

    def get_checkpoint_snapshot(self, checkpoint_id: str) -> tuple[Checkpoint, dict[str, Any]] | None:
        rows = self._query("SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,))
        if not rows:
            return None
        row = rows[0]
        checkpoint = self._row_to_checkpoint(row)
        return checkpoint, loads(row["snapshot_json"], {})

    def list_checkpoints(self, pid: str | None = None, limit: int | None = None) -> list[Checkpoint]:
        params: list[Any] = []
        sql = "SELECT * FROM checkpoints"
        if pid is not None:
            sql += " WHERE pid = ?"
            params.append(pid)
        sql += " ORDER BY created_at DESC, checkpoint_id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._row_to_checkpoint(row) for row in self._query(sql, params)]

    def snapshot_tables(self) -> dict[str, list[dict[str, Any]]]:
        raise RuntimeError(
            "full-table SQLite snapshots are disabled; use CheckpointManager.create for scoped durable checkpoints"
        )

    def restore_tables(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        raise RuntimeError(
            "full-table SQLite restore is disabled; use CheckpointManager.restore to preserve append-only history"
        )

    def _ensure_process_schema(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(processes)")}
        if "working_directory" not in columns:
            self.conn.execute("ALTER TABLE processes ADD COLUMN working_directory TEXT NOT NULL DEFAULT '.'")
        if "resource_usage_json" not in columns:
            self.conn.execute("ALTER TABLE processes ADD COLUMN resource_usage_json TEXT NOT NULL DEFAULT '{}'")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_processes_status_created ON processes(status, created_at, pid)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_processes_parent_created ON processes(parent_pid, created_at, pid)"
        )

    def _ensure_resource_reservation_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS process_resource_reservations (
              parent_pid TEXT NOT NULL,
              child_pid TEXT NOT NULL,
              reservation_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(parent_pid, child_pid)
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_resource_reservations_parent
              ON process_resource_reservations(parent_pid, child_pid)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_resource_reservations_child
              ON process_resource_reservations(child_pid, parent_pid)
            """
        )

    def _ensure_capability_schema(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(capabilities)")}
        if "max_delegation_depth" not in columns:
            self.conn.execute("ALTER TABLE capabilities ADD COLUMN max_delegation_depth INTEGER")

    def _ensure_audit_schema(self) -> None:
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_records_created ON audit_records(timestamp, record_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_records_actor_created ON audit_records(actor, timestamp, record_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_records_target_created ON audit_records(target, timestamp, record_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_records_correlation_created ON audit_records(correlation_id, timestamp, record_id)"
        )

    def _ensure_llm_call_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_calls (
              call_id TEXT PRIMARY KEY,
              pid TEXT,
              image_id TEXT,
              purpose TEXT NOT NULL,
              status TEXT NOT NULL,
              api TEXT,
              model TEXT,
              request_id TEXT,
              response_id TEXT,
              messages_json TEXT NOT NULL,
              tools_json TEXT NOT NULL,
              request_options_json TEXT NOT NULL,
              response_content TEXT NOT NULL,
              tool_calls_json TEXT NOT NULL,
              reasoning_json TEXT,
              usage_json TEXT NOT NULL,
              raw_response_json TEXT,
              observability_json TEXT NOT NULL DEFAULT '{}',
              error TEXT,
              created_at TEXT NOT NULL,
              completed_at TEXT
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_pid_created ON llm_calls(pid, created_at)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_request_id ON llm_calls(request_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_response_id ON llm_calls(response_id)")
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(llm_calls)")}
        if "observability_json" not in columns:
            self.conn.execute("ALTER TABLE llm_calls ADD COLUMN observability_json TEXT NOT NULL DEFAULT '{}'")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_pending_actions (
              pid TEXT PRIMARY KEY,
              wait_type TEXT NOT NULL,
              request_id TEXT,
              child_pid TEXT,
              filters_json TEXT NOT NULL,
              action_json TEXT NOT NULL,
              content_preview TEXT NOT NULL,
              tool_call_count INTEGER NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )

    def _ensure_process_message_schema(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(process_messages)")}
        if "channel" not in columns:
            self.conn.execute("ALTER TABLE process_messages ADD COLUMN channel TEXT NOT NULL DEFAULT 'default'")
        if "correlation_id" not in columns:
            self.conn.execute("ALTER TABLE process_messages ADD COLUMN correlation_id TEXT")
        if "reply_to" not in columns:
            self.conn.execute("ALTER TABLE process_messages ADD COLUMN reply_to TEXT")
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_process_messages_recipient_status_kind
              ON process_messages(recipient_pid, status, kind, channel, created_at)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_process_messages_correlation
              ON process_messages(recipient_pid, correlation_id, status, created_at)
            """
        )

    def _ensure_object_task_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS object_tasks (
              task_id TEXT PRIMARY KEY,
              owner_oid TEXT NOT NULL,
              creator_pid TEXT NOT NULL,
              runner_pid TEXT,
              tool TEXT NOT NULL,
              tool_id TEXT,
              status TEXT NOT NULL,
              notification_json TEXT NOT NULL,
              owner_watch_json TEXT NOT NULL,
              result_oid TEXT,
              error TEXT,
              wait_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              started_at TEXT,
              completed_at TEXT
            )
            """
        )
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(object_tasks)")}
        if "owner_watch_json" not in columns:
            self.conn.execute("ALTER TABLE object_tasks ADD COLUMN owner_watch_json TEXT NOT NULL DEFAULT '{}'")
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_object_tasks_owner_status
              ON object_tasks(owner_oid, status, updated_at)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_object_tasks_creator_status
              ON object_tasks(creator_pid, status, updated_at)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_object_tasks_runner
              ON object_tasks(runner_pid)
            """
        )

    def _ensure_checkpoint_schema(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(checkpoints)")}
        if "created_by" not in columns:
            self.conn.execute("ALTER TABLE checkpoints ADD COLUMN created_by TEXT")
        if "snapshot_version" not in columns:
            self.conn.execute("ALTER TABLE checkpoints ADD COLUMN snapshot_version INTEGER NOT NULL DEFAULT 1")
        if "metadata_json" not in columns:
            self.conn.execute("ALTER TABLE checkpoints ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")

    def _ensure_external_effect_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_effects (
              effect_id TEXT PRIMARY KEY,
              record_id TEXT,
              event_id TEXT,
              pid TEXT NOT NULL,
              provider TEXT NOT NULL,
              operation TEXT NOT NULL,
              target TEXT,
              rollback_class TEXT NOT NULL,
              rollback_status TEXT NOT NULL,
              state_mutation INTEGER NOT NULL,
              information_flow INTEGER NOT NULL,
              provider_metadata_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_external_effects_created ON external_effects(created_at, effect_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_external_effects_pid_created ON external_effects(pid, created_at, effect_id)"
        )

    def _ensure_skill_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS skills (
              skill_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              version TEXT NOT NULL,
              package_json TEXT NOT NULL,
              source_type TEXT NOT NULL,
              source TEXT,
              package_sha256 TEXT NOT NULL,
              registered_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_skills_name
              ON skills(name);

            CREATE TABLE IF NOT EXISTS skill_trust (
              trust_id TEXT PRIMARY KEY,
              source_type TEXT NOT NULL,
              source TEXT NOT NULL,
              package_sha256 TEXT NOT NULL,
              trusted_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              UNIQUE(source_type, source, package_sha256)
            );
            """
        )

    def _ensure_tool_candidate_schema(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(tool_candidates)")}
        if "registered_tool_id" not in columns:
            self.conn.execute("ALTER TABLE tool_candidates ADD COLUMN registered_tool_id TEXT")

    def _ensure_jsonrpc_endpoint_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jsonrpc_endpoints (
              endpoint_id TEXT PRIMARY KEY,
              spec_json TEXT NOT NULL,
              registered_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )

    def _ensure_runtime_module_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_modules (
              module_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              version TEXT NOT NULL,
              entrypoint TEXT NOT NULL,
              manifest_path TEXT NOT NULL,
              manifest_sha256 TEXT NOT NULL,
              source_path TEXT NOT NULL,
              source_sha256 TEXT NOT NULL,
              status TEXT NOT NULL,
              loaded_at TEXT,
              registered_json TEXT NOT NULL,
              error TEXT,
              metadata_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )

    def _ensure_object_namespace_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS object_namespaces (
              namespace TEXT PRIMARY KEY,
              parent_namespace TEXT,
              metadata_json TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(objects)")}
        if "namespace" not in columns or self._has_name_only_unique_index():
            self._rebuild_objects_table_with_namespace(columns)
        elif "name" not in columns:
            self.conn.execute("ALTER TABLE objects ADD COLUMN name TEXT")
            self.conn.execute("UPDATE objects SET name = oid WHERE name IS NULL OR name = ''")
        self.conn.execute("DROP INDEX IF EXISTS idx_objects_name")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_objects_namespace_name ON objects(namespace, name)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_objects_created_by ON objects(created_by, created_at, oid)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_objects_namespace_updated ON objects(namespace, updated_at, created_at, oid)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_objects_namespace_type_updated ON objects(namespace, type, updated_at, created_at, oid)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_object_namespaces_created_by ON object_namespaces(created_by, namespace)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_object_namespaces_parent ON object_namespaces(parent_namespace, namespace)"
        )
        now = utc_now()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO object_namespaces (
                namespace, parent_namespace, metadata_json, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (self.SYSTEM_NAMESPACE, None, dumps({"kind": "root"}), "runtime", now, now),
        )
        namespaces = {
            str(row["namespace"] or self.SYSTEM_NAMESPACE)
            for row in self.conn.execute("SELECT DISTINCT namespace FROM objects")
        }
        for namespace in sorted(namespaces):
            for current in self._namespace_chain(namespace):
                if current == self.SYSTEM_NAMESPACE:
                    continue
                parent = current.rsplit("/", 1)[0] if "/" in current else None
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO object_namespaces (
                        namespace, parent_namespace, metadata_json, created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (current, parent, dumps({"kind": "migration"}), "storage.migration", now, now),
                )

    def _rebuild_objects_table_with_namespace(self, columns: set[str]) -> None:
        self.conn.execute("DROP INDEX IF EXISTS idx_objects_name")
        self.conn.execute("ALTER TABLE objects RENAME TO objects_old")
        self.conn.execute(
            """
            CREATE TABLE objects (
              oid TEXT PRIMARY KEY,
              namespace TEXT NOT NULL DEFAULT 'system',
              name TEXT NOT NULL,
              type TEXT NOT NULL,
              schema_version TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              provenance_json TEXT NOT NULL,
              version INTEGER NOT NULL,
              immutable INTEGER NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        namespace_expr = "namespace" if "namespace" in columns else f"'{self.SYSTEM_NAMESPACE}'"
        name_expr = "name" if "name" in columns else "oid"
        self.conn.execute(
            f"""
            INSERT INTO objects (
                oid, namespace, name, type, schema_version, payload_json, metadata_json,
                provenance_json, version, immutable, created_by, created_at, updated_at
            )
            SELECT
                oid, COALESCE({namespace_expr}, '{self.SYSTEM_NAMESPACE}'), COALESCE({name_expr}, oid), type,
                schema_version, payload_json, metadata_json, provenance_json, version,
                immutable, created_by, created_at, updated_at
            FROM objects_old
            """
        )
        self.conn.execute("DROP TABLE objects_old")

    def _has_name_only_unique_index(self) -> bool:
        for index in self.conn.execute("PRAGMA index_list(objects)"):
            if not bool(index["unique"]):
                continue
            columns = [row["name"] for row in self.conn.execute(f"PRAGMA index_info({index['name']})")]
            if columns == ["name"]:
                return True
        return False

    def _namespace_chain(self, namespace: str) -> list[str]:
        parts = namespace.split("/")
        return ["/".join(parts[:index]) for index in range(1, len(parts) + 1)]

    def _memory_payload_marker(self, present: bool) -> dict[str, Any]:
        return {"storage": "runtime_memory", "present": present}

    def _process_params(self, process: AgentProcess) -> tuple[Any, ...]:
        return (
            process.pid,
            process.parent_pid,
            process.image_id,
            process.status.value,
            process.goal_oid,
            dumps(process.memory_view) if process.memory_view else None,
            dumps(process.capabilities),
            dumps(process.loaded_skills),
            dumps(process.tool_table),
            process.event_cursor,
            process.checkpoint_head,
            process.status_message,
            dumps(process.resource_budget),
            dumps(process.resource_usage),
            process.working_directory,
            process.created_at,
            process.updated_at,
        )

    def _object_task_params(self, task: ObjectTask) -> tuple[Any, ...]:
        return (
            task.task_id,
            task.owner_oid,
            task.creator_pid,
            task.runner_pid,
            task.tool,
            task.tool_id,
            task.status.value,
            dumps(task.notification),
            dumps(task.owner_watch),
            task.result_oid,
            task.error,
            dumps(task.wait),
            task.created_at,
            task.updated_at,
            task.started_at,
            task.completed_at,
        )

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    def _runtime_module_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = self._row_to_dict(row)
        data["registered"] = loads(data.pop("registered_json"), {})
        data["metadata"] = loads(data.pop("metadata_json"), {})
        return data

    def _image_row_metadata(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "registered_by": row["registered_by"],
            "source": row["source"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _dict_to_agent_image(self, data: dict[str, Any]) -> AgentImage:
        item = dict(data)
        item.setdefault("boot", {"kind": "fresh"})
        return AgentImage(**item)

    def _row_to_object(self, row: sqlite3.Row) -> AgentObject:
        metadata = ObjectMetadata(**loads(row["metadata_json"], {}))
        provenance = Provenance(**loads(row["provenance_json"], {}))
        return AgentObject(
            oid=row["oid"],
            namespace=row["namespace"],
            name=row["name"],
            type=ObjectType(row["type"]),
            schema_version=row["schema_version"],
            payload=self._object_payloads[row["oid"]],
            metadata=metadata,
            provenance=provenance,
            version=row["version"],
            immutable=bool(row["immutable"]),
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_namespace(self, row: sqlite3.Row) -> ObjectNamespace:
        return ObjectNamespace(
            namespace=row["namespace"],
            parent_namespace=row["parent_namespace"],
            metadata=loads(row["metadata_json"], {}),
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_link(self, row: sqlite3.Row) -> ObjectLink:
        return ObjectLink(
            link_id=row["id"],
            src=row["src_oid"],
            relation=RelationType(row["relation"]),
            dst=row["dst_oid"],
            metadata=loads(row["metadata_json"], {}),
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    def _row_to_process(self, row: sqlite3.Row) -> AgentProcess:
        return AgentProcess(
            pid=row["pid"],
            parent_pid=row["parent_pid"],
            image_id=row["image_id"],
            status=ProcessStatus(row["status"]),
            goal_oid=row["goal_oid"],
            memory_view=self._dict_to_view(loads(row["memory_view_json"])) if row["memory_view_json"] else None,
            capabilities=loads(row["capabilities_json"], []),
            loaded_skills=loads(row["loaded_skills_json"], {}),
            tool_table=loads(row["tool_table_json"], {}),
            event_cursor=row["event_cursor"],
            checkpoint_head=row["checkpoint_head"],
            resource_budget=ResourceBudget(**loads(row["resource_budget_json"], {})),
            resource_usage=ResourceUsage(**loads(row["resource_usage_json"] if "resource_usage_json" in row.keys() else None, {})),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            working_directory=row["working_directory"] if "working_directory" in row.keys() else ".",
            status_message=row["status_message"],
        )

    def _row_to_resource_reservation(self, row: sqlite3.Row) -> ResourceReservation:
        return ResourceReservation(
            parent_pid=row["parent_pid"],
            child_pid=row["child_pid"],
            reserved={key: float(value) for key, value in loads(row["reservation_json"], {}).items()},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            type=EventType(row["type"]),
            source=row["source"],
            target=row["target"],
            payload=loads(row["payload_json"], {}),
            priority=EventPriority(row["priority"]),
            created_at=row["created_at"],
            correlation_id=row["correlation_id"],
            causality=loads(row["causality_json"], {}),
        )

    def _row_to_capability(self, row: sqlite3.Row) -> Capability:
        keys = set(row.keys())
        return Capability(
            cap_id=row["cap_id"],
            subject=row["subject"],
            resource=row["resource"],
            rights=set(loads(row["rights_json"], [])),
            constraints=loads(row["constraints_json"], {}),
            issued_by=row["issued_by"],
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            delegable=bool(row["delegable"]),
            revocable=bool(row["revocable"]),
            effect=CapabilityEffect(row["effect"]) if "effect" in keys else CapabilityEffect.ALLOW,
            issuer_cap_id=row["issuer_cap_id"] if "issuer_cap_id" in keys else None,
            parent_cap_id=row["parent_cap_id"] if "parent_cap_id" in keys else None,
            delegation_depth=int(row["delegation_depth"]) if "delegation_depth" in keys else 0,
            max_delegation_depth=(
                int(row["max_delegation_depth"])
                if "max_delegation_depth" in keys and row["max_delegation_depth"] is not None
                else None
            ),
            uses_remaining=row["uses_remaining"] if "uses_remaining" in keys else None,
            status=(
                CapabilityStatus(row["status"])
                if "status" in keys
                else (CapabilityStatus.REVOKED if bool(row["revoked"]) else CapabilityStatus.ACTIVE)
            ),
            metadata=loads(row["metadata_json"], {}) if "metadata_json" in keys else {},
        )

    def _row_to_audit(self, row: sqlite3.Row) -> AuditRecord:
        return AuditRecord(
            record_id=row["record_id"],
            timestamp=row["timestamp"],
            actor=row["actor"],
            action=row["action"],
            target=row["target"],
            input_refs=loads(row["input_refs_json"], []),
            output_refs=loads(row["output_refs_json"], []),
            capability_refs=loads(row["capability_refs_json"], []),
            decision=loads(row["decision_json"]) if row["decision_json"] else None,
            correlation_id=row["correlation_id"],
            parent_record_id=row["parent_record_id"],
        )

    def _row_to_external_effect(self, row: sqlite3.Row) -> ExternalEffectRecord:
        return ExternalEffectRecord(
            effect_id=row["effect_id"],
            record_id=row["record_id"],
            event_id=row["event_id"],
            pid=row["pid"],
            provider=row["provider"],
            operation=row["operation"],
            target=row["target"],
            rollback_class=ExternalEffectRollbackClass(row["rollback_class"]),
            rollback_status=ExternalEffectRollbackStatus(row["rollback_status"]),
            state_mutation=bool(row["state_mutation"]),
            information_flow=bool(row["information_flow"]),
            provider_metadata=loads(row["provider_metadata_json"], {}),
            created_at=row["created_at"],
        )

    def _row_to_checkpoint(self, row: sqlite3.Row) -> Checkpoint:
        keys = set(row.keys())
        return Checkpoint(
            checkpoint_id=row["checkpoint_id"],
            pid=row["pid"],
            reason=row["reason"],
            created_at=row["created_at"],
            created_by=row["created_by"] if "created_by" in keys else None,
            snapshot_version=int(row["snapshot_version"]) if "snapshot_version" in keys else 1,
            metadata=loads(row["metadata_json"], {}) if "metadata_json" in keys else {},
        )

    def _row_to_human_request(self, row: sqlite3.Row) -> HumanRequest:
        return HumanRequest(
            request_id=row["request_id"],
            pid=row["pid"],
            human=row["human"],
            payload=loads(row["payload_json"], {}),
            status=HumanRequestStatus(row["status"]),
            decision=loads(row["decision_json"]) if row["decision_json"] else None,
            blocking=bool(row["blocking"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_llm_call(self, row: sqlite3.Row) -> LLMCallRecord:
        return LLMCallRecord(
            call_id=row["call_id"],
            pid=row["pid"],
            image_id=row["image_id"],
            purpose=row["purpose"],
            status=row["status"],
            api=row["api"],
            model=row["model"],
            request_id=row["request_id"],
            response_id=row["response_id"],
            messages=loads(row["messages_json"], []),
            tools=loads(row["tools_json"], []),
            request_options=loads(row["request_options_json"], {}),
            response_content=row["response_content"],
            tool_calls=loads(row["tool_calls_json"], []),
            reasoning=loads(row["reasoning_json"]) if row["reasoning_json"] else None,
            usage=loads(row["usage_json"], {}),
            raw_response=loads(row["raw_response_json"]) if row["raw_response_json"] else None,
            observability=loads(row["observability_json"], {}),
            error=row["error"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    def _row_to_llm_pending_action(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "pid": row["pid"],
            "wait_type": row["wait_type"],
            "request_id": row["request_id"],
            "child_pid": row["child_pid"],
            "filters": loads(row["filters_json"], {}),
            "action": loads(row["action_json"], {}),
            "content_preview": row["content_preview"],
            "tool_call_count": row["tool_call_count"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_object_task(self, row: sqlite3.Row) -> ObjectTask:
        raw_notification = loads(row["notification_json"], {})
        if isinstance(raw_notification.get("status"), str):
            raw_notification["status"] = ObjectTaskNotificationStatus(raw_notification["status"])
        notification = ObjectTaskNotification(**raw_notification)
        raw_owner_watch = loads(row["owner_watch_json"], {})
        owner_watch = ObjectTaskOwnerWatch(**raw_owner_watch)
        return ObjectTask(
            task_id=row["task_id"],
            owner_oid=row["owner_oid"],
            creator_pid=row["creator_pid"],
            runner_pid=row["runner_pid"],
            tool=row["tool"],
            tool_id=row["tool_id"],
            status=ObjectTaskStatus(row["status"]),
            notification=notification,
            owner_watch=owner_watch,
            result_oid=row["result_oid"],
            error=row["error"],
            wait=loads(row["wait_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    def _llm_call_limit(self, limit: int | None) -> int:
        selected = _LLM_DEFAULTS.call_record_list_limit if limit is None else int(limit)
        if selected <= 0:
            raise ValidationError("llm call limit must be positive")
        if selected > _LLM_DEFAULTS.call_record_hard_limit:
            raise ValidationError(f"llm call limit exceeds hard cap {_LLM_DEFAULTS.call_record_hard_limit}")
        return selected

    def _row_to_process_message(self, row: sqlite3.Row) -> ProcessMessage:
        return ProcessMessage(
            message_id=row["message_id"],
            sender=row["sender"],
            recipient_pid=row["recipient_pid"],
            kind=ProcessMessageKind(row["kind"]),
            channel=row["channel"] if "channel" in row.keys() else "default",
            correlation_id=row["correlation_id"] if "correlation_id" in row.keys() else None,
            reply_to=row["reply_to"] if "reply_to" in row.keys() else None,
            subject=row["subject"],
            body=row["body"],
            payload=loads(row["payload_json"], {}),
            status=ProcessMessageStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            acked_at=row["acked_at"],
        )

    def _row_to_tool_candidate(self, row: sqlite3.Row) -> ToolCandidate:
        return ToolCandidate(
            candidate_id=row["candidate_id"],
            pid=row["pid"],
            spec=self._dict_to_tool_spec(loads(row["spec_json"], {})),
            source_code=row["source_code"],
            tests=loads(row["tests_json"], []),
            requested_capabilities=loads(row["requested_capabilities_json"], []),
            status=ToolCandidateStatus(row["status"]),
            validation=loads(row["validation_json"]) if row["validation_json"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            registered_tool_id=row["registered_tool_id"] if "registered_tool_id" in row.keys() else None,
        )

    def _skill_row_metadata(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "source_type": row["source_type"],
            "source": row["source"],
            "package_sha256": row["package_sha256"],
            "registered_by": row["registered_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _jsonrpc_endpoint_row_metadata(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "registered_by": row["registered_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _dict_to_jsonrpc_endpoint(self, data: dict[str, Any]) -> JsonRpcEndpointSpec:
        return JsonRpcEndpointSpec(
            schema_version=int(data.get("schema_version", 1)),
            endpoint_id=data["endpoint_id"],
            url=data["url"],
            headers={
                str(name): JsonRpcHeaderSpec(
                    env=str(value["env"]),
                    prefix=str(value.get("prefix", "")),
                    suffix=str(value.get("suffix", "")),
                )
                for name, value in dict(data.get("headers") or {}).items()
            },
            methods=[
                JsonRpcMethodSpec(
                    method_id=item["method_id"],
                    rpc_method=item["rpc_method"],
                    right=item["right"],
                    rollback_class=item["rollback_class"],
                    rollback_status=item.get("rollback_status"),
                    state_mutation=bool(item["state_mutation"]),
                    information_flow=bool(item["information_flow"]),
                    params_schema=dict(item.get("params_schema") or {}),
                    metadata=dict(item.get("metadata") or {}),
                )
                for item in list(data.get("methods") or [])
            ],
            timeout_s=float(data["timeout_s"]),
            max_request_bytes=int(data["max_request_bytes"]),
            max_response_bytes=int(data["max_response_bytes"]),
            metadata=dict(data.get("metadata") or {}),
        )

    def _dict_to_skill_package(self, data: dict[str, Any]) -> SkillPackage:
        return SkillPackage(
            schema_version=int(data.get("schema_version", 1)),
            skill_id=data["skill_id"],
            name=data["name"],
            description=data.get("description", ""),
            instructions=data.get("instructions", ""),
            version=data.get("version", "v0"),
            license=data.get("license", ""),
            compatibility=data.get("compatibility", ""),
            metadata={str(key): str(value) for key, value in dict(data.get("metadata", {})).items()},
            allowed_tools=list(data.get("allowed_tools", [])),
            actions=[ActionSchema(**item) for item in data.get("actions", [])],
            jit_tools=[JitToolSpec(**item) for item in data.get("jit_tools", [])],
            required_capabilities=list(data.get("required_capabilities", [])),
            resources=[SkillResource(**item) for item in data.get("resources", [])],
            package_sha256=data.get("package_sha256", ""),
            diagnostics=list(data.get("diagnostics", [])),
        )

    def _dict_to_tool_spec(self, data: dict[str, Any]) -> ToolSpec:
        return ToolSpec(
            name=data["name"],
            description=data.get("description", ""),
            version=data.get("version", "1.0.0"),
            input_schema=data.get("input_schema", {}),
            output_schema=data.get("output_schema", {}),
            policy=data.get("policy", {}),
            tags=data.get("tags", []),
            metadata=data.get("metadata", {}),
            required_capabilities=data.get("required_capabilities", []),
            side_effects=data.get("side_effects", []),
        )

    def _dict_to_view(self, data: dict[str, Any]) -> MemoryView:
        return MemoryView(
            view_id=data["view_id"],
            owner_pid=data["owner_pid"],
            roots=[self._dict_to_handle(item) for item in data.get("roots", [])],
            filters=[self._dict_to_filter(item) for item in data.get("filters", [])],
            rights_policy=data.get("rights_policy", "inherit"),
            created_from=data.get("created_from"),
            mode=ViewMode(data.get("mode", ViewMode.READ_ONLY.value)),
        )

    def _dict_to_filter(self, data: dict[str, Any]) -> ObjectFilter:
        return ObjectFilter(
            type=ObjectType(data["type"]) if data.get("type") else None,
            tags=data.get("tags", []),
            text=data.get("text"),
        )

    def _dict_to_handle(self, data: dict[str, Any]) -> ObjectHandle:
        return ObjectHandle(
            oid=data["oid"],
            rights=set(data.get("rights", [])),
            capability_id=data["capability_id"],
            expires_at=data.get("expires_at"),
        )

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from agent_libos.models import (
    AgentObject,
    AgentProcess,
    AuditRecord,
    Capability,
    Checkpoint,
    Event,
    EventPriority,
    EventType,
    HumanRequest,
    HumanRequestStatus,
    MemoryView,
    ObjectFilter,
    ObjectHandle,
    ObjectLink,
    ObjectMetadata,
    ObjectType,
    ProcessStatus,
    Provenance,
    RelationType,
    ResourceBudget,
    ToolCandidate,
    ToolCandidateStatus,
    ToolHandle,
    ToolSpec,
    ViewMode,
)
from agent_libos.serde import dumps, loads


class SQLiteStore:
    """Small SQLite repository used by the MVP runtime.

    The store is intentionally thin: policy, permissions, and process semantics
    live in managers. This layer only owns durable shape and reconstruction.
    """

    SNAPSHOT_TABLES = [
        "objects",
        "object_links",
        "processes",
        "events",
        "capabilities",
        "human_requests",
        "tools",
        "tool_candidates",
    ]

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.initialize()

    def close(self) -> None:
        self.conn.close()

    def initialize(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS objects (
                  oid TEXT PRIMARY KEY,
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
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

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
                  revoked INTEGER NOT NULL
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

                CREATE TABLE IF NOT EXISTS checkpoints (
                  checkpoint_id TEXT PRIMARY KEY,
                  pid TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  snapshot_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
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
                  validation_json TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                """
            )
            self.conn.commit()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, tuple(params)))

    def insert_object(self, obj: AgentObject) -> None:
        self._execute(
            """
            INSERT INTO objects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obj.oid,
                obj.type.value,
                obj.schema_version,
                dumps(obj.payload),
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
        self._execute(
            """
            UPDATE objects
               SET type = ?, schema_version = ?, payload_json = ?, metadata_json = ?,
                   provenance_json = ?, version = ?, immutable = ?, created_by = ?,
                   created_at = ?, updated_at = ?
             WHERE oid = ?
            """,
            (
                obj.type.value,
                obj.schema_version,
                dumps(obj.payload),
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
        return self._row_to_object(rows[0]) if rows else None

    def list_objects(self) -> list[AgentObject]:
        return [self._row_to_object(row) for row in self._query("SELECT * FROM objects")]

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
            INSERT INTO processes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                   status_message = ?, resource_budget_json = ?, created_at = ?,
                   updated_at = ?
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
            "INSERT INTO capabilities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                int(cap.revoked),
            ),
        )

    def update_capability(self, cap: Capability) -> None:
        self._execute(
            """
            UPDATE capabilities
               SET subject = ?, resource = ?, rights_json = ?, constraints_json = ?,
                   issued_by = ?, issued_at = ?, expires_at = ?, delegable = ?,
                   revocable = ?, revoked = ?
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
                int(cap.revoked),
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

    def list_audit(self, limit: int | None = None) -> list[AuditRecord]:
        sql = "SELECT * FROM audit_records ORDER BY timestamp"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        return [self._row_to_audit(row) for row in self._query(sql, params)]

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

    def get_tool_spec(self, tool_id: str) -> ToolSpec | None:
        rows = self._query("SELECT * FROM tools WHERE tool_id = ?", (tool_id,))
        if not rows:
            return None
        return self._dict_to_tool_spec(loads(rows[0]["spec_json"]))

    def list_tools(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._query("SELECT * FROM tools ORDER BY created_at")]

    def insert_tool_candidate(self, candidate: ToolCandidate) -> None:
        self._execute(
            "INSERT INTO tool_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate.candidate_id,
                candidate.pid,
                dumps(candidate.spec),
                candidate.source_code,
                dumps(candidate.tests),
                dumps(candidate.requested_capabilities),
                candidate.status.value,
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
                   requested_capabilities_json = ?, status = ?, validation_json = ?,
                   created_at = ?, updated_at = ?
             WHERE candidate_id = ?
            """,
            (
                candidate.pid,
                dumps(candidate.spec),
                candidate.source_code,
                dumps(candidate.tests),
                dumps(candidate.requested_capabilities),
                candidate.status.value,
                dumps(candidate.validation) if candidate.validation is not None else None,
                candidate.created_at,
                candidate.updated_at,
                candidate.candidate_id,
            ),
        )

    def get_tool_candidate(self, candidate_id: str) -> ToolCandidate | None:
        rows = self._query("SELECT * FROM tool_candidates WHERE candidate_id = ?", (candidate_id,))
        return self._row_to_tool_candidate(rows[0]) if rows else None

    def insert_checkpoint(self, checkpoint: Checkpoint, snapshot: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?)",
            (
                checkpoint.checkpoint_id,
                checkpoint.pid,
                checkpoint.reason,
                dumps(snapshot),
                checkpoint.created_at,
            ),
        )

    def get_checkpoint_snapshot(self, checkpoint_id: str) -> tuple[Checkpoint, dict[str, Any]] | None:
        rows = self._query("SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,))
        if not rows:
            return None
        row = rows[0]
        checkpoint = Checkpoint(
            checkpoint_id=row["checkpoint_id"],
            pid=row["pid"],
            reason=row["reason"],
            created_at=row["created_at"],
        )
        return checkpoint, loads(row["snapshot_json"], {})

    def snapshot_tables(self) -> dict[str, list[dict[str, Any]]]:
        snapshot: dict[str, list[dict[str, Any]]] = {}
        with self._lock:
            for table in self.SNAPSHOT_TABLES:
                snapshot[table] = [dict(row) for row in self.conn.execute(f"SELECT * FROM {table}")]
        return snapshot

    def restore_tables(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        with self._lock:
            cur = self.conn.cursor()
            for table in self.SNAPSHOT_TABLES:
                cur.execute(f"DELETE FROM {table}")
                rows = snapshot.get(table, [])
                if not rows:
                    continue
                columns = list(rows[0].keys())
                placeholders = ", ".join("?" for _ in columns)
                col_sql = ", ".join(columns)
                for row in rows:
                    cur.execute(
                        f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
                        tuple(row[column] for column in columns),
                    )
            self.conn.commit()

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
            process.created_at,
            process.updated_at,
        )

    def _row_to_object(self, row: sqlite3.Row) -> AgentObject:
        metadata = ObjectMetadata(**loads(row["metadata_json"], {}))
        provenance = Provenance(**loads(row["provenance_json"], {}))
        return AgentObject(
            oid=row["oid"],
            type=ObjectType(row["type"]),
            schema_version=row["schema_version"],
            payload=loads(row["payload_json"]),
            metadata=metadata,
            provenance=provenance,
            version=row["version"],
            immutable=bool(row["immutable"]),
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
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status_message=row["status_message"],
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
            revoked=bool(row["revoked"]),
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

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Iterable, Protocol


class StoreTransaction(Protocol):
    """Backend-neutral SQL cursor surface used by scoped store helpers."""

    rowcount: int

    def execute(self, sql: str, params: Iterable[Any] = ()) -> "StoreTransaction":
        ...

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> None:
        ...

    def fetchone(self) -> Any | None:
        ...


class RuntimeStore(Protocol):
    """Durable runtime store contract consumed by managers and primitives."""

    config: Any
    path: str

    def close(self) -> None:
        ...

    def locked(self) -> AbstractContextManager[None]:
        ...

    def transaction(self, *, include_object_payloads: bool = False) -> AbstractContextManager[StoreTransaction]:
        ...

    def validate_table_identifier(self, table: str) -> str:
        ...

    def validate_column_identifier(self, table: str, column: str) -> str:
        ...

    def payload_marker(self, *, present: bool) -> dict[str, Any]:
        ...

    def object_payload(self, oid: str) -> Any:
        ...

    def set_object_payload(self, oid: str, payload: Any) -> None:
        ...

    def forget_object_payload(self, oid: str) -> None:
        ...

    def has_object_payload(self, oid: str) -> bool:
        ...

    def snapshot_object_payloads(self, oids: Iterable[str]) -> dict[str, Any]:
        ...

    def claim_runnable_process(self, pid: str) -> Any | None:
        ...

    def insert_object(self, obj: Any) -> None:
        ...

    def update_object(
        self,
        obj: Any,
        *,
        expected_version: int | None = None,
        expected_owner_kind: Any | None = None,
        expected_owner_id: str | None = None,
    ) -> bool:
        ...

    def get_object(self, oid: str) -> Any | None:
        ...

    def get_object_by_name(self, name: str, namespace: str) -> Any | None:
        ...

    def get_object_ref_by_name(self, name: str, namespace: str) -> dict[str, Any] | None:
        ...

    def object_name_exists(self, name: str, namespace: str, except_oid: str | None = None) -> bool:
        ...

    def list_objects(self, namespace: str | None = None) -> list[Any]:
        ...

    def list_objects_owned_by(self, owner_kind: Any, owner_id: str) -> list[Any]:
        ...

    def list_object_oids_owned_by(self, owner_kind: Any, owner_id: str) -> list[str]:
        ...

    def delete_object(
        self,
        oid: str,
        *,
        expected_version: int | None = None,
        expected_owner_kind: Any | None = None,
        expected_owner_id: str | None = None,
    ) -> bool:
        ...

    def insert_namespace(self, namespace: Any) -> None:
        ...

    def get_namespace(self, namespace: str) -> Any | None:
        ...

    def namespace_exists(self, namespace: str) -> bool:
        ...

    def list_namespaces(self, parent_namespace: str | None = None) -> list[Any]:
        ...

    def list_namespaces_created_by(self, created_by: str) -> list[Any]:
        ...

    def insert_link(self, link: Any) -> None:
        ...

    def insert_process(self, process: Any) -> None:
        ...

    def update_process(self, process: Any) -> None:
        ...

    def get_process(self, pid: str) -> Any | None:
        ...

    def list_processes(self) -> list[Any]:
        ...

    def list_processes_by_status(self, status: Any) -> list[Any]:
        ...

    def list_child_processes(self, parent_pid: str) -> list[Any]:
        ...

    def upsert_resource_reservation(self, reservation: Any) -> None:
        ...

    def get_resource_reservation(self, parent_pid: str, child_pid: str) -> Any | None:
        ...

    def list_resource_reservations(self, *, parent_pid: str | None = None, child_pid: str | None = None) -> list[Any]:
        ...

    def delete_resource_reservation(self, parent_pid: str, child_pid: str) -> None:
        ...

    def delete_resource_reservations_for_process(self, pid: str) -> None:
        ...

    def select_table_rows(
        self,
        table: str,
        where_sql: str = "",
        params: Iterable[Any] = (),
        *,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        ...

    def insert_table_row(self, table: str, row: dict[str, Any]) -> None:
        ...

    def delete_table_rows(self, table: str, where_sql: str, params: Iterable[Any] = ()) -> None:
        ...

    def insert_event(self, event: Any) -> None:
        ...

    def list_events(self, target: str | None = None) -> list[Any]:
        ...

    def insert_capability(self, cap: Any) -> None:
        ...

    def consume_capability_uses(self, cap_id: str, count: int = 1) -> Any | None:
        ...

    def reserve_capability_uses(
        self,
        cap_id: str,
        reservation_id: str,
        *,
        count: int = 1,
        reserved_by: str,
        reason: str,
        created_at: str,
    ) -> Any | None:
        ...

    def commit_capability_use_reservation(self, reservation_id: str, *, updated_at: str) -> bool:
        ...

    def restore_capability_use_reservation(self, reservation_id: str, *, updated_at: str) -> Any | None:
        ...

    def update_capability(self, cap: Any) -> None:
        ...

    def get_capability(self, cap_id: str) -> Any | None:
        ...

    def list_capabilities(self, subject: str | None = None) -> list[Any]:
        ...

    def insert_audit(self, record: Any) -> None:
        ...

    def list_audit(self, **filters: Any) -> list[Any]:
        ...

    def insert_external_effect(self, record: Any) -> None:
        ...

    def finalize_external_effect(self, intent_effect_id: str, record: Any) -> bool:
        ...

    def abandon_external_effect_intent(self, effect_id: str) -> bool:
        ...

    def list_external_effects(self, **filters: Any) -> list[Any]:
        ...

    def insert_human_request(self, request: Any) -> None:
        ...

    def update_human_request(self, request: Any) -> None:
        ...

    def get_human_request(self, request_id: str) -> Any | None:
        ...

    def list_human_requests(self, **filters: Any) -> list[Any]:
        ...

    def insert_llm_call(self, record: Any) -> None:
        ...

    def list_llm_calls(self, pid: str | None = None, limit: int | None = None) -> list[Any]:
        ...

    def get_latest_llm_call(self, *, pid: str, purpose: str | None = None) -> Any | None:
        ...

    def upsert_llm_tool_output(
        self,
        *,
        pid: str,
        response_id: str,
        call_id: str,
        tool_name: str | None,
        output: str,
    ) -> None:
        ...

    def list_llm_tool_outputs(self, *, pid: str, response_id: str) -> list[dict[str, Any]]:
        ...

    def get_llm_context_generation(self, pid: str) -> str:
        ...

    def set_llm_context_generation(self, pid: str, generation: str) -> None:
        ...

    def upsert_llm_pending_action(self, pid: str, pending: dict[str, Any]) -> None:
        ...

    def get_llm_pending_action(self, pid: str) -> dict[str, Any] | None:
        ...

    def list_llm_pending_actions(self, *, status: str | None = "pending") -> list[dict[str, Any]]:
        ...

    def claim_llm_pending_action(self, pid: str, *, resume_token: str) -> dict[str, Any] | None:
        ...

    def complete_llm_pending_action(self, pid: str, *, resume_token: str) -> bool:
        ...

    def insert_process_message(self, message: Any) -> None:
        ...

    def update_process_message(self, message: Any) -> None:
        ...

    def list_process_messages(self, pid: str, **filters: Any) -> list[Any]:
        ...

    def insert_object_task(self, task: Any) -> None:
        ...

    def update_object_task(self, task: Any) -> None:
        ...

    def get_object_task(self, task_id: str) -> Any | None:
        ...

    def list_object_tasks(self, **filters: Any) -> list[Any]:
        ...

    def mark_object_tasks_abandoned(self, reason: str) -> list[str]:
        ...

    def upsert_agent_rating(self, rating: Any) -> Any:
        ...

    def get_agent_rating(self, pid: str, rater: str, source: str = "gui") -> Any | None:
        ...

    def insert_tool(self, handle: Any, spec: Any, registered_by: str, created_at: str, ephemeral: bool) -> None:
        ...

    def update_tool(self, handle: Any, spec: Any, registered_by: str, ephemeral: bool) -> None:
        ...

    def delete_tool(self, tool_id: str, *, registered_by: str | None = None) -> None:
        ...

    def get_tool_spec(self, tool_id: str) -> Any | None:
        ...

    def list_tools(self) -> list[dict[str, Any]]:
        ...

    def insert_tool_candidate(self, candidate: Any) -> None:
        ...

    def update_tool_candidate(self, candidate: Any) -> None:
        ...

    def get_tool_candidate(self, candidate_id: str) -> Any | None:
        ...

    def upsert_skill(self, *args: Any, **kwargs: Any) -> None:
        ...

    def get_skill(self, skill_id: str) -> Any | None:
        ...

    def list_skills(self, text: str | None = None, limit: int | None = None) -> list[Any]:
        ...

    def insert_skill_trust(self, *args: Any, **kwargs: Any) -> None:
        ...

    def delete_skill_trust(self, *, source_type: str, source: str, package_sha256: str) -> None:
        ...

    def is_skill_trusted(self, *, source_type: str, source: str, package_sha256: str) -> bool:
        ...

    def upsert_jsonrpc_endpoint(self, endpoint: Any, *, registered_by: str, created_at: str) -> None:
        ...

    def get_jsonrpc_endpoint(self, endpoint_id: str) -> Any | None:
        ...

    def list_jsonrpc_endpoints(self, text: str | None = None, limit: int | None = None) -> list[Any]:
        ...

    def delete_jsonrpc_endpoint(self, endpoint_id: str) -> None:
        ...

    def upsert_mcp_server(self, server: Any, *, registered_by: str, created_at: str) -> None:
        ...

    def get_mcp_server(self, server_id: str) -> Any | None:
        ...

    def list_mcp_servers(self, text: str | None = None, limit: int | None = None) -> list[Any]:
        ...

    def delete_mcp_server(self, server_id: str) -> None:
        ...

    def upsert_image(self, image: Any, *, registered_by: str, source: str | None, created_at: str) -> None:
        ...

    def get_image(self, image_id: str) -> Any | None:
        ...

    def list_images(self) -> list[Any]:
        ...

    def delete_image(self, image_id: str, *, registered_by: str | None = None) -> None:
        ...

    def insert_image_artifact(self, *args: Any, **kwargs: Any) -> None:
        ...

    def get_image_artifact(self, artifact_id: str) -> Any | None:
        ...

    def upsert_runtime_module(self, *args: Any, **kwargs: Any) -> None:
        ...

    def get_runtime_module(self, module_id: str) -> dict[str, Any] | None:
        ...

    def list_runtime_modules(self, limit: int | None = None) -> list[dict[str, Any]]:
        ...

    def insert_checkpoint(self, checkpoint: Any, snapshot: dict[str, Any]) -> None:
        ...

    def get_checkpoint_snapshot(self, checkpoint_id: str) -> Any | None:
        ...

    def list_checkpoints(self, pid: str | None = None, limit: int | None = None) -> list[Any]:
        ...

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, ClassVar, Protocol


class _StoreBoundary(Protocol):
    """Transaction boundary needed by a unit of work.

    Repository methods are deliberately delegated from an explicit allowlist,
    so this protocol does not repeat the concrete SQL store surface.
    """

    def locked(self) -> AbstractContextManager[None]:
        ...

    def transaction(self, *, include_object_payloads: bool = False) -> AbstractContextManager[Any]:
        ...


class _RepositoryFacade:
    """Narrow domain adapter over a shared transactional SQL engine."""

    _METHODS: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, store: _StoreBoundary) -> None:
        self.__store = store

    def __getattr__(self, name: str) -> Any:
        if name not in self._METHODS:
            raise AttributeError(f"{type(self).__name__!s} has no repository operation {name!r}")
        return getattr(self.__store, name)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | self._METHODS)

    def _delegate(self, name: str, *args: Any, **kwargs: Any) -> Any:
        return getattr(self.__store, name)(*args, **kwargs)

    def locked(self) -> AbstractContextManager[None]:
        return self.__store.locked()

    def transaction(self, *, include_object_payloads: bool = False) -> AbstractContextManager[Any]:
        return self.__store.transaction(include_object_payloads=include_object_payloads)


class ProcessRepository(_RepositoryFacade):
    """Process lifecycle, messaging, human, and LLM persistence."""

    _METHODS = frozenset(
        {
            "claim_runnable_process",
            "claim_execution",
            "complete_execution",
            "release_execution",
            "recover_stale_executions",
            "insert_process",
            "patch_process",
            "transition_process",
            "append_process_memory_roots",
            "remove_process_memory_roots",
            "append_process_capability_ids",
            "patch_process_tool_tables",
            "remove_process_tool_bindings",
            "replace_process_for_restore",
            "insert_runtime_publication",
            "get_runtime_publication",
            "list_runtime_publications",
            "advance_runtime_publication",
            "get_process",
            "list_processes",
            "get_processes_with_ancestors",
            "list_processes_by_status",
            "list_child_processes",
            "insert_human_request",
            "update_human_request",
            "get_human_request",
            "list_human_requests",
            "insert_llm_call",
            "list_llm_calls",
            "get_llm_call",
            "get_latest_llm_call",
            "upsert_llm_tool_output",
            "list_llm_tool_outputs",
            "get_llm_context_generation",
            "set_llm_context_generation",
            "get_llm_context_label_history",
            "merge_llm_context_label_history",
            "upsert_llm_pending_action",
            "get_llm_pending_action",
            "list_llm_pending_actions",
            "claim_llm_pending_action",
            "complete_llm_pending_action",
            "insert_process_message",
            "update_process_message",
            "update_process_message_metadata",
            "get_process_message",
            "list_process_messages",
            "get_process_activity_summaries",
            "insert_object_task",
            "update_object_task",
            "get_object_task",
            "list_object_tasks",
            "mark_object_tasks_abandoned",
            "upsert_agent_rating",
            "get_agent_rating",
            "get_agent_ratings_for_processes",
            "list_agent_ratings",
        }
    )


class ObjectRepository(_RepositoryFacade):
    """Object metadata, volatile payload, namespace, and link persistence."""

    _METHODS = frozenset(
        {
            "payload_marker",
            "object_payload",
            "set_object_payload",
            "forget_object_payload",
            "has_object_payload",
            "is_recovered_object_payload",
            "snapshot_object_payloads",
            "insert_object",
            "update_object",
            "get_object",
            "get_object_by_name",
            "get_object_ref_by_name",
            "object_name_exists",
            "list_objects",
            "list_object_oids_created_by",
            "list_objects_created_by",
            "list_objects_owned_by",
            "list_object_oids_owned_by",
            "delete_object",
            "insert_namespace",
            "get_namespace",
            "namespace_exists",
            "list_namespaces",
            "list_namespaces_created_by",
            "insert_link",
            "list_links",
            "select_table_rows",
        }
    )


class AuthorityRepository(_RepositoryFacade):
    """Authority, reservation, capability, and data-flow persistence."""

    _METHODS = frozenset(
        {
            "insert_authority_manifest",
            "get_authority_manifest",
            "get_authority_manifest_for_process",
            "list_authority_manifests",
            "upsert_resource_reservation",
            "get_resource_reservation",
            "list_resource_reservations",
            "delete_resource_reservation",
            "delete_resource_reservations_for_process",
            "insert_capability",
            "consume_capability_uses",
            "reserve_capability_uses",
            "commit_capability_use_reservation",
            "restore_capability_use_reservation",
            "get_capability_use_reservation",
            "update_capability",
            "get_capability",
            "list_capabilities",
            "get_process",
            "append_process_capability_ids",
            "register_sink_trust",
            "unregister_sink_trust",
            "get_sink_trust",
            "inspect_sink_trust",
            "list_sink_trust",
            "get_sink_trust_generation",
            "insert_data_flow_decision",
            "get_data_flow_decision",
            "list_data_flow_decisions",
            "upsert_file_label_binding",
            "get_file_label_binding",
            "get_file_label_binding_by_id",
            "get_file_label_binding_generation",
            "list_file_label_bindings",
            "list_file_label_bindings_for_tree",
            "tombstone_file_label_binding",
        }
    )


class EvidenceRepository(_RepositoryFacade):
    """Append-only events, audit, operations, and external effects."""

    _METHODS = frozenset(
        {
            "insert_event",
            "list_events",
            "get_event",
            "insert_audit",
            "list_audit",
            "get_audit",
            "insert_operation",
            "get_operation",
            "list_operations",
            "update_operation",
            "insert_operation_evidence",
            "list_operation_evidence",
            "insert_context_materialization_manifest",
            "get_context_materialization_manifest",
            "list_context_materialization_manifests",
            "insert_external_effect",
            "finalize_external_effect",
            "transition_external_effect",
            "abandon_external_effect_intent",
            "list_external_effects",
            "get_external_effect",
        }
    )


class ExtensionRepository(_RepositoryFacade):
    """Tools, Skills, providers, images, modules, and checkpoints."""

    _METHODS = frozenset(
        {
            "insert_tool",
            "update_tool",
            "delete_tool",
            "get_tool_spec",
            "list_tools",
            "insert_tool_candidate",
            "update_tool_candidate",
            "get_tool_candidate",
            "upsert_skill",
            "get_skill",
            "list_skills",
            "insert_skill_trust",
            "delete_skill_trust",
            "is_skill_trusted",
            "list_skill_trust",
            "upsert_jsonrpc_endpoint",
            "get_jsonrpc_endpoint",
            "list_jsonrpc_endpoints",
            "delete_jsonrpc_endpoint",
            "upsert_mcp_server",
            "get_mcp_server",
            "list_mcp_servers",
            "delete_mcp_server",
            "upsert_image",
            "get_image",
            "list_images",
            "delete_image",
            "insert_image_artifact",
            "get_image_artifact",
            "list_image_artifacts",
            "upsert_runtime_module",
            "get_runtime_module",
            "list_runtime_modules",
            "insert_checkpoint",
            "get_checkpoint_snapshot",
            "list_checkpoints",
        }
    )

    def delete_tool_candidate(self, candidate_id: str, pid: str) -> None:
        self._delegate(
            "delete_table_rows",
            "tool_candidates",
            "candidate_id = ? AND pid = ?",
            (candidate_id, pid),
        )

    def delete_jit_tool_rows(self, pid: str, tool_ids: Iterator[str] | set[str]) -> None:
        for tool_id in set(tool_ids):
            self._delegate(
                "delete_table_rows",
                "tools",
                "tool_id = ? AND ephemeral = 1",
                (tool_id,),
            )
            self._delegate(
                "delete_table_rows",
                "tool_candidates",
                "pid = ? AND registered_tool_id = ?",
                (pid, tool_id),
            )

    def list_registered_tool_candidate_rows(self) -> list[dict[str, Any]]:
        """Return durable sources that are eligible for runtime rehydration."""

        return self._delegate(
            "select_table_rows",
            "tool_candidates",
            "status = ?",
            ("registered",),
            order_by="updated_at, candidate_id",
        )

    def list_tool_candidate_rows_for_registration(
        self,
        pid: str,
        tool_id: str,
    ) -> list[dict[str, Any]]:
        return self._delegate(
            "select_table_rows",
            "tool_candidates",
            "pid = ? AND registered_tool_id = ?",
            (pid, tool_id),
            order_by="candidate_id",
        )


class ProtectedEffectRepository:
    """Cross-repository view used by the protected-operation SDK.

    Effect evidence and capability reservations remain in their owning
    repositories while sharing the UnitOfWork transaction boundary.
    """

    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self.__unit_of_work = unit_of_work

    def transaction(self, *, include_object_payloads: bool = False) -> AbstractContextManager[Any]:
        return self.__unit_of_work.transaction(
            include_object_payloads=include_object_payloads
        )

    def insert_external_effect(self, *args: Any, **kwargs: Any) -> Any:
        return self.__unit_of_work.evidence.insert_external_effect(*args, **kwargs)

    def get_external_effect(self, *args: Any, **kwargs: Any) -> Any:
        return self.__unit_of_work.evidence.get_external_effect(*args, **kwargs)

    def list_external_effects(self, *args: Any, **kwargs: Any) -> Any:
        return self.__unit_of_work.evidence.list_external_effects(*args, **kwargs)

    def finalize_external_effect(self, *args: Any, **kwargs: Any) -> Any:
        return self.__unit_of_work.evidence.finalize_external_effect(*args, **kwargs)

    def transition_external_effect(self, *args: Any, **kwargs: Any) -> Any:
        return self.__unit_of_work.evidence.transition_external_effect(*args, **kwargs)

    def abandon_external_effect_intent(self, *args: Any, **kwargs: Any) -> Any:
        return self.__unit_of_work.evidence.abandon_external_effect_intent(*args, **kwargs)

    def get_capability_use_reservation(self, *args: Any, **kwargs: Any) -> Any:
        return self.__unit_of_work.authority.get_capability_use_reservation(
            *args,
            **kwargs,
        )

    def list_operation_evidence(self, *args: Any, **kwargs: Any) -> Any:
        return self.__unit_of_work.evidence.list_operation_evidence(*args, **kwargs)

    def get_operation(self, *args: Any, **kwargs: Any) -> Any:
        return self.__unit_of_work.evidence.get_operation(*args, **kwargs)

    def get_capability(self, *args: Any, **kwargs: Any) -> Any:
        return self.__unit_of_work.authority.get_capability(*args, **kwargs)


class UnitOfWork:
    """One transaction/lock boundary shared by the five repositories.

    It does not own or initialize the store.  Existing helpers keep their
    nested-savepoint behavior, so calls through multiple repositories remain
    part of the outer transaction opened here.
    """

    def __init__(self, store: _StoreBoundary) -> None:
        self.__store: _StoreBoundary = store
        self.processes = ProcessRepository(store)
        self.objects = ObjectRepository(store)
        self.authority = AuthorityRepository(store)
        self.evidence = EvidenceRepository(store)
        self.extensions = ExtensionRepository(store)
        self.protected_effects = ProtectedEffectRepository(self)

    def locked(self) -> AbstractContextManager[None]:
        return self.__store.locked()

    @contextmanager
    def transaction(self, *, include_object_payloads: bool = False) -> Iterator[UnitOfWork]:
        with self.__store.transaction(include_object_payloads=include_object_payloads):
            yield self

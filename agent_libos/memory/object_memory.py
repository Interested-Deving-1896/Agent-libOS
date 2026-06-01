from __future__ import annotations

from dataclasses import replace
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models.exceptions import CapabilityDenied, NotFound, ValidationError
from agent_libos.utils.ids import estimate_tokens, new_id, utc_now
from agent_libos.models import (
    EventType,
    MaterializedContext,
    MemoryView,
    MemoryViewSpec,
    MergePolicy,
    MergeResult,
    ObjectNamespace,
    ObjectFilter,
    ObjectHandle,
    ObjectLink,
    ObjectMetadata,
    ObjectPatch,
    ObjectQuery,
    ObjectRight,
    ObjectType,
    Provenance,
    RelationType,
    ViewMode,
    AgentObject,
)
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.storage import SQLiteStore


class ObjectMemoryManager:
    """Typed Object Memory with capability-checked handles and namespace-local names."""

    def __init__(
        self,
        store: SQLiteStore,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        config: AgentLibOSConfig | None = None,
    ):
        self.config = config or DEFAULT_CONFIG
        self.store = store
        self.capabilities = capabilities
        self.audit = audit
        self.events = events

    def create_object(
        self,
        pid: str,
        object_type: ObjectType | str,
        payload: Any,
        metadata: ObjectMetadata | None = None,
        immutable: bool = True,
        provenance: Provenance | None = None,
        name: str | None = None,
        namespace: str | None = None,
    ) -> ObjectHandle:
        now = utc_now()
        obj_type = ObjectType(object_type)
        oid = new_id("obj")
        object_namespace = self.resolve_namespace(pid, namespace)
        object_name = self._normalize_name(name or self._default_name(obj_type, oid))
        self._require_namespace_exists(object_namespace)
        self._require_namespace_right(pid, object_namespace, "write")
        # Names are stable namespace directory entries, not authority. Reads by
        # name still resolve to an oid and pass through object capability checks.
        self._require_unique_name(object_name, object_namespace)
        meta = metadata or ObjectMetadata(token_estimate=estimate_tokens(payload))
        if meta.token_estimate is None:
            meta.token_estimate = estimate_tokens(payload)
        obj = AgentObject(
            oid=oid,
            namespace=object_namespace,
            name=object_name,
            type=obj_type,
            schema_version=self.config.memory.object_schema_version,
            payload=payload,
            metadata=meta,
            provenance=provenance or Provenance(created_from_action="memory.create_object"),
            version=1,
            immutable=immutable,
            created_by=pid,
            created_at=now,
            updated_at=now,
        )
        self.store.insert_object(obj)
        rights = {
            ObjectRight.READ.value,
            ObjectRight.LINK.value,
            ObjectRight.DIFF.value,
            ObjectRight.MATERIALIZE.value,
            ObjectRight.DELETE.value,
            ObjectRight.GRANT.value,
        }
        if not immutable:
            rights.add(ObjectRight.WRITE.value)
        handle = self.capabilities.handle_for_object(pid, obj.oid, rights, issued_by="memory")
        self.events.emit(
            EventType.OBJECT_CREATED,
            source=pid,
            target=pid,
            payload={
                "oid": obj.oid,
                "namespace": obj.namespace,
                "name": obj.name,
                "qualified_name": self.qualified_name(obj),
                "type": obj.type.value,
            },
        )
        self.audit.record(
            actor=pid,
            action="memory.create_object",
            target=f"object:{obj.oid}",
            output_refs=[obj.oid],
            capability_refs=[handle.capability_id],
            decision={"namespace": obj.namespace, "name": obj.name, "type": obj.type.value},
        )
        return handle

    def process_namespace(self, pid: str) -> str:
        return f"{self.config.memory.process_namespace_prefix}:{pid}"

    def resolve_namespace(self, pid: str, namespace: str | None = None) -> str:
        if namespace is None:
            return self.process_namespace(pid)
        return self._normalize_namespace(namespace)

    def ensure_process_namespace(self, pid: str, parent_pid: str | None = None) -> ObjectNamespace:
        namespace_name = self.process_namespace(pid)
        existing = self.store.get_namespace(namespace_name)
        if existing is None:
            now = utc_now()
            namespace = ObjectNamespace(
                namespace=namespace_name,
                parent_namespace=None,
                metadata={"kind": "process", "pid": pid, "parent_pid": parent_pid},
                created_by=pid,
                created_at=now,
                updated_at=now,
            )
            self.store.insert_namespace(namespace)
            existing = namespace
            self.audit.record(
                actor=pid,
                action="memory.ensure_process_namespace",
                target=self._namespace_resource(namespace_name),
                decision={"namespace": namespace_name, "parent_pid": parent_pid, "created": True},
            )
        self.capabilities.grant(
            subject=pid,
            resource=self._namespace_resource(namespace_name),
            rights=["read", "write", "admin"],
            issued_by="memory.process_namespace",
        )
        return existing

    def create_namespace(
        self,
        pid: str,
        namespace: str,
        parent_namespace: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ObjectNamespace:
        namespace_name = self._normalize_namespace(namespace)
        if self.store.namespace_exists(namespace_name):
            raise ValidationError(f"Object Memory namespace already exists: {namespace_name}")
        parent = self._normalize_namespace(parent_namespace) if parent_namespace else self._parent_namespace(namespace_name)
        if parent is not None:
            self._require_namespace_exists(parent)
            self._require_namespace_right(pid, parent, "write")
        now = utc_now()
        ns = ObjectNamespace(
            namespace=namespace_name,
            parent_namespace=parent,
            metadata=dict(metadata or {}),
            created_by=pid,
            created_at=now,
            updated_at=now,
        )
        self.store.insert_namespace(ns)
        self.capabilities.grant(
            subject=pid,
            resource=self._namespace_resource(namespace_name),
            rights=["read", "write", "admin"],
            issued_by="memory.namespace",
        )
        self.audit.record(
            actor=pid,
            action="memory.create_namespace",
            target=self._namespace_resource(namespace_name),
            decision={"namespace": namespace_name, "parent_namespace": parent},
        )
        return ns

    def get_namespace(self, pid: str, namespace: str | None = None) -> ObjectNamespace:
        namespace_name = self.resolve_namespace(pid, namespace)
        ns = self.store.get_namespace(namespace_name)
        if ns is None:
            raise NotFound(f"Object Memory namespace not found: {namespace_name}")
        self._require_namespace_right(pid, namespace_name, "read")
        self.audit.record(
            actor=pid,
            action="memory.get_namespace",
            target=self._namespace_resource(namespace_name),
            decision={"namespace": namespace_name},
        )
        return ns

    def list_namespace(self, pid: str, namespace: str | None = None) -> dict[str, Any]:
        namespace_name = self.resolve_namespace(pid, namespace)
        self._require_namespace_exists(namespace_name)
        self._require_namespace_right(pid, namespace_name, "read")
        objects = [
            obj
            for obj in self.store.list_objects(namespace=namespace_name)
            if self.capabilities.check(pid, f"object:{obj.oid}", ObjectRight.READ)
        ]
        child_namespaces = [
            ns
            for ns in self.store.list_namespaces(parent_namespace=namespace_name)
            if self._can_read_namespace(pid, ns.namespace)
        ]
        self.audit.record(
            actor=pid,
            action="memory.list_namespace",
            target=self._namespace_resource(namespace_name),
            output_refs=[obj.oid for obj in objects],
            decision={"namespace": namespace_name, "objects": len(objects), "namespaces": len(child_namespaces)},
        )
        return {
            "namespace": namespace_name,
            "objects": objects,
            "namespaces": child_namespaces,
        }

    def get_object(self, pid: str, handle: ObjectHandle) -> AgentObject:
        self.capabilities.assert_handle(pid, handle, ObjectRight.READ)
        obj = self.store.get_object(handle.oid)
        if obj is None:
            raise NotFound(f"object not found: {handle.oid}")
        self.audit.record(
            actor=pid,
            action="memory.get_object",
            target=f"object:{handle.oid}",
            input_refs=[handle.oid],
            capability_refs=[handle.capability_id],
        )
        return obj

    def get_object_by_name(self, pid: str, name: str, namespace: str | None = None) -> AgentObject:
        object_namespace = self.resolve_namespace(pid, namespace)
        object_name = self._normalize_name(name)
        obj = self.store.get_object_by_name(object_name, namespace=object_namespace)
        if obj is None:
            raise NotFound(f"object not found: {self.qualified_name_parts(object_namespace, object_name)}")
        # Name lookup never bypasses the object capability model.
        self._require_namespace_right(pid, object_namespace, "read")
        self.capabilities.require(pid, f"object:{obj.oid}", ObjectRight.READ)
        self.audit.record(
            actor=pid,
            action="memory.get_object_by_name",
            target=f"object:{self.qualified_name(obj)}",
            input_refs=[obj.oid],
            decision={"namespace": obj.namespace, "name": obj.name, "oid": obj.oid},
        )
        return obj

    def handle_for_name(
        self,
        pid: str,
        name: str,
        rights: set[str] | list[str] | tuple[str, ...] | None = None,
        issued_by: str = "memory.name",
        namespace: str | None = None,
    ) -> ObjectHandle:
        object_namespace = self.resolve_namespace(pid, namespace)
        object_name = self._normalize_name(name)
        obj = self.store.get_object_by_name(object_name, namespace=object_namespace)
        if obj is None:
            raise NotFound(f"object not found: {self.qualified_name_parts(object_namespace, object_name)}")
        requested = {str(right) for right in (rights or {ObjectRight.READ.value})}
        # A name can be resolved only into rights the process already has.
        self._require_namespace_right(pid, object_namespace, "read")
        for right in requested:
            self.capabilities.require(pid, f"object:{obj.oid}", right)
        handle = self.capabilities.handle_for_object(pid, obj.oid, requested, issued_by=issued_by)
        self.audit.record(
            actor=pid,
            action="memory.handle_for_name",
            target=f"object:{self.qualified_name(obj)}",
            output_refs=[obj.oid],
            capability_refs=[handle.capability_id],
            decision={"namespace": obj.namespace, "name": obj.name, "rights": sorted(requested)},
        )
        return handle

    def update_object(self, pid: str, handle: ObjectHandle, patch: ObjectPatch) -> ObjectHandle:
        self.capabilities.assert_handle(pid, handle, ObjectRight.WRITE)
        current = self.store.get_object(handle.oid)
        if current is None:
            raise NotFound(f"object not found: {handle.oid}")
        if current.immutable:
            raise CapabilityDenied(f"immutable object cannot be updated: {handle.oid}")
        next_namespace = current.namespace
        next_name = current.name
        if patch.namespace is not None:
            next_namespace = self._normalize_namespace(patch.namespace)
            self._require_namespace_exists(next_namespace)
            self._require_namespace_right(pid, next_namespace, "write")
        if patch.name is not None:
            next_name = self._normalize_name(patch.name)
        if next_namespace != current.namespace or next_name != current.name:
            self._require_namespace_right(pid, current.namespace, "write")
            self._require_unique_name(next_name, next_namespace, except_oid=current.oid)
        updated = replace(
            current,
            namespace=next_namespace,
            name=next_name,
            payload=current.payload if patch.payload is None else patch.payload,
            metadata=current.metadata if patch.metadata is None else patch.metadata,
            provenance=current.provenance if patch.provenance is None else patch.provenance,
            version=current.version + 1,
            updated_at=utc_now(),
        )
        self.store.update_object(updated)
        self.events.emit(
            EventType.OBJECT_UPDATED,
            source=pid,
            target=pid,
            payload={
                "oid": updated.oid,
                "namespace": updated.namespace,
                "name": updated.name,
                "qualified_name": self.qualified_name(updated),
                "version": updated.version,
            },
        )
        self.audit.record(
            actor=pid,
            action="memory.update_object",
            target=f"object:{updated.oid}",
            input_refs=[updated.oid],
            output_refs=[updated.oid],
            capability_refs=[handle.capability_id],
            decision={"namespace": updated.namespace, "name": updated.name, "version": updated.version},
        )
        return handle

    def link_objects(
        self,
        pid: str,
        src: ObjectHandle,
        relation: RelationType | str,
        dst: ObjectHandle,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.capabilities.assert_handle(pid, src, ObjectRight.LINK)
        self.capabilities.assert_handle(pid, dst, ObjectRight.READ)
        link = ObjectLink(
            link_id=new_id("lnk"),
            src=src.oid,
            relation=RelationType(relation),
            dst=dst.oid,
            metadata=metadata or {},
            created_by=pid,
            created_at=utc_now(),
        )
        self.store.insert_link(link)
        self.events.emit(
            EventType.OBJECT_LINKED,
            source=pid,
            target=pid,
            payload={"src": src.oid, "relation": link.relation.value, "dst": dst.oid},
        )
        self.audit.record(
            actor=pid,
            action="memory.link_objects",
            target=f"object:{src.oid}",
            input_refs=[src.oid, dst.oid],
            capability_refs=[src.capability_id, dst.capability_id],
            decision={"relation": link.relation.value},
        )

    def query_objects(self, pid: str, query: ObjectQuery) -> list[ObjectHandle]:
        results: list[ObjectHandle] = []
        namespace = self.resolve_namespace(pid, query.namespace)
        self._require_namespace_right(pid, namespace, "read")
        for obj in self.store.list_objects(namespace=namespace):
            if query.name is not None and obj.name != self._normalize_name(query.name):
                continue
            if query.type is not None and obj.type.value != str(query.type):
                continue
            if query.tags and not set(query.tags).issubset(set(obj.metadata.tags)):
                continue
            if query.text and query.text.lower() not in self._search_text(obj).lower():
                continue
            if not self.capabilities.check(pid, f"object:{obj.oid}", ObjectRight.READ):
                continue
            rights = {"read", "materialize", "link", "diff"}
            handle = self.capabilities.handle_for_object(pid, obj.oid, rights, issued_by="memory.query")
            results.append(handle)
            if len(results) >= query.limit:
                break
        self.audit.record(
            actor=pid,
            action="memory.query_objects",
            target=f"object_namespace:{namespace}",
            output_refs=[handle.oid for handle in results],
            decision={"count": len(results), "namespace": namespace},
        )
        return results

    def create_view(
        self,
        pid: str,
        roots: list[ObjectHandle],
        mode: ViewMode | str = ViewMode.READ_ONLY,
        filters: list[ObjectFilter] | None = None,
    ) -> MemoryView:
        view_mode = ViewMode(mode)
        for handle in roots:
            self.capabilities.assert_handle(pid, handle, ObjectRight.READ)
        view = MemoryView(
            view_id=new_id("view"),
            owner_pid=pid,
            roots=roots,
            filters=filters or [],
            rights_policy="attenuate" if view_mode == ViewMode.READ_ONLY else "inherit",
            created_from=None,
            mode=view_mode,
        )
        self.audit.record(
            actor=pid,
            action="memory.create_view",
            target=f"view:{view.view_id}",
            input_refs=[handle.oid for handle in roots],
            capability_refs=[handle.capability_id for handle in roots],
            decision={"mode": view.mode.value},
        )
        return view

    def fork_view(
        self,
        parent_pid: str,
        child_pid: str,
        parent_view: MemoryView,
        spec: MemoryViewSpec | None = None,
    ) -> MemoryView:
        spec = spec or MemoryViewSpec()
        source_roots = spec.roots if spec.roots is not None else parent_view.roots
        if not spec.include_parent_roots and spec.roots is None:
            source_roots = []
        child_roots: list[ObjectHandle] = []
        for handle in source_roots:
            self.capabilities.assert_handle(parent_pid, handle, ObjectRight.READ)
            rights = spec.rights
            if rights is None:
                rights = {"read", "materialize", "diff"}
                if spec.mode in {ViewMode.MUTABLE, ViewMode.COPY_ON_WRITE} and "write" in handle.rights:
                    rights.add("write")
            child_roots.append(
                self.capabilities.handle_for_object(
                    child_pid,
                    handle.oid,
                    rights,
                    issued_by=f"process:{parent_pid}:fork",
                )
            )
        view = MemoryView(
            view_id=new_id("view"),
            owner_pid=child_pid,
            roots=child_roots,
            filters=list(parent_view.filters),
            rights_policy="fork_attenuated",
            created_from=parent_view.view_id,
            mode=spec.mode,
        )
        self.audit.record(
            actor=parent_pid,
            action="memory.fork_view",
            target=f"view:{view.view_id}",
            input_refs=[handle.oid for handle in source_roots],
            output_refs=[handle.oid for handle in child_roots],
            capability_refs=[handle.capability_id for handle in child_roots],
            decision={"child_pid": child_pid, "mode": view.mode.value},
        )
        return view

    def merge_view(
        self,
        parent_pid: str,
        child_view: MemoryView,
        policy: MergePolicy | None = None,
    ) -> MergeResult:
        policy = policy or MergePolicy()
        merged: list[str] = []
        skipped: list[str] = []
        candidate_oids = {handle.oid for handle in child_view.roots}
        if policy.include_child_created:
            candidate_oids.update(
                obj.oid for obj in self.store.list_objects() if obj.created_by == child_view.owner_pid
            )
        for oid in sorted(candidate_oids):
            obj = self.store.get_object(oid)
            if obj is None:
                skipped.append(oid)
                continue
            self.capabilities.handle_for_object(
                parent_pid,
                oid,
                policy.grant_rights,
                issued_by=f"memory.merge:{child_view.owner_pid}",
            )
            merged.append(oid)
        self.audit.record(
            actor=parent_pid,
            action="memory.merge_view",
            target=f"view:{child_view.view_id}",
            input_refs=sorted(candidate_oids),
            output_refs=merged,
            decision={"merged": len(merged), "skipped": skipped},
        )
        return MergeResult(merged_oids=merged, skipped_oids=skipped)

    def snapshot_view(self, pid: str, view: MemoryView) -> str:
        snapshot_id = new_id("snap")
        self.audit.record(
            actor=pid,
            action="memory.snapshot_view",
            target=f"snapshot:{snapshot_id}",
            input_refs=[handle.oid for handle in view.roots],
            decision={"view_id": view.view_id},
        )
        return snapshot_id

    def release_process_owned(self, pid: str, preserve_oids: set[str] | None = None) -> list[str]:
        # Process-owned Object payloads behave like volatile memory: they are
        # reclaimed on exit unless explicitly promoted as the process result.
        preserve = set(preserve_oids or set())
        released: list[str] = []
        for oid in self.store.list_object_oids_created_by(pid):
            if oid in preserve:
                obj = self.store.get_object(oid)
                if obj is not None:
                    self.store.update_object(replace(obj, created_by=f"process_result:{pid}"))
                continue
            self.store.delete_object(oid)
            released.append(oid)
        if released or preserve:
            self.audit.record(
                actor="memory",
                action="memory.release_process_owned",
                target=f"process:{pid}",
                input_refs=released,
                output_refs=sorted(preserve),
                decision={"released": released, "preserved": sorted(preserve)},
            )
        return released

    def materialize_context(
        self,
        pid: str,
        view: MemoryView,
        policy: str | None = None,
        budget_tokens: int | None = None,
    ) -> MaterializedContext:
        selected_policy = policy or self.config.memory.context_policy
        selected_budget = budget_tokens if budget_tokens is not None else self.config.memory.materialize_budget_tokens
        objects: list[AgentObject] = []
        omitted: list[str] = []
        for handle in view.roots:
            try:
                self.capabilities.assert_handle(pid, handle, ObjectRight.MATERIALIZE)
                obj = self.store.get_object(handle.oid)
                if obj is not None:
                    objects.append(obj)
            except CapabilityDenied:
                omitted.append(handle.oid)

        objects = self._sort_for_policy(objects, selected_policy)
        chunks: list[str] = []
        refs: list[str] = []
        total = 0
        for obj in objects:
            tokens = obj.metadata.token_estimate or estimate_tokens(obj.payload)
            if total + tokens > selected_budget:
                omitted.append(obj.oid)
                continue
            chunks.append(self._render_object(obj))
            refs.append(obj.oid)
            total += tokens
        context = MaterializedContext(
            text="\n\n".join(chunks),
            object_refs=refs,
            token_count=total,
            omitted_objects=omitted,
            policy_used=selected_policy,
        )
        self.audit.record(
            actor=pid,
            action="memory.materialize_context",
            target=f"view:{view.view_id}",
            input_refs=[handle.oid for handle in view.roots],
            output_refs=refs,
            decision={"tokens": total, "omitted": omitted, "policy": selected_policy},
        )
        return context

    def _search_text(self, obj: AgentObject) -> str:
        return " ".join(
            [
                obj.namespace,
                obj.name,
                obj.metadata.title or "",
                obj.metadata.summary or "",
                " ".join(obj.metadata.tags),
                repr(obj.payload),
            ]
        )

    def _sort_for_policy(self, objects: list[AgentObject], policy: str) -> list[AgentObject]:
        if policy == "recency_first":
            return sorted(objects, key=lambda obj: obj.updated_at, reverse=True)
        if policy == "evidence_first":
            return sorted(objects, key=lambda obj: obj.type != ObjectType.EVIDENCE)
        if policy == "plan_first":
            priority = {ObjectType.GOAL: 0, ObjectType.TASK: 1, ObjectType.PLAN: 2, ObjectType.STEP: 3}
            return sorted(objects, key=lambda obj: priority.get(obj.type, 10))
        if policy == "error_debug":
            priority = {ObjectType.ERROR_TRACE: 0, ObjectType.TEST_RESULT: 1, ObjectType.CODE_PATCH: 2}
            return sorted(objects, key=lambda obj: priority.get(obj.type, 10))
        return objects

    def _render_object(self, obj: AgentObject) -> str:
        title = f" title={obj.metadata.title!r}" if obj.metadata.title else ""
        summary = f"\nsummary: {obj.metadata.summary}" if obj.metadata.summary else ""
        return (
            f"[{obj.oid}] namespace={obj.namespace!r} name={obj.name!r} "
            f"qualified_name={self.qualified_name(obj)!r} type={obj.type.value} "
            f"version={obj.version}{title}{summary}\npayload: {obj.payload!r}"
        )

    def qualified_name(self, obj: AgentObject) -> str:
        return self.qualified_name_parts(obj.namespace, obj.name)

    def qualified_name_parts(self, namespace: str, name: str) -> str:
        return f"{namespace}/{name}"

    def _default_name(self, object_type: ObjectType, oid: str) -> str:
        return f"{object_type.value}:{oid}"

    def _normalize_namespace(self, namespace: str) -> str:
        normalized = namespace.strip().replace("\\", "/").strip("/")
        if not normalized:
            raise ValidationError("Object Memory namespace must be non-empty")
        segments = normalized.split("/")
        if any(not segment or segment in {".", ".."} or segment.strip() != segment for segment in segments):
            raise ValidationError(f"invalid Object Memory namespace: {namespace}")
        return normalized

    def _normalize_name(self, name: str) -> str:
        normalized = name.strip()
        if not normalized:
            raise ValidationError("object name must be non-empty")
        return normalized

    def _parent_namespace(self, namespace: str) -> str | None:
        if "/" not in namespace:
            return None
        return namespace.rsplit("/", 1)[0]

    def _require_namespace_exists(self, namespace: str) -> None:
        if not self.store.namespace_exists(namespace):
            raise NotFound(f"Object Memory namespace not found: {namespace}")

    def _namespace_resource(self, namespace: str) -> str:
        return f"object_namespace:{namespace}"

    def _require_namespace_right(
        self,
        pid: str,
        namespace: str,
        right: str,
    ) -> None:
        self.capabilities.require(pid, self._namespace_resource(namespace), right)

    def _can_read_namespace(self, pid: str, namespace: str) -> bool:
        return self.capabilities.check(
            pid,
            self._namespace_resource(namespace),
            "read",
        )

    def _require_unique_name(self, name: str, namespace: str, except_oid: str | None = None) -> None:
        if self.store.object_name_exists(name, except_oid=except_oid, namespace=namespace):
            raise ValidationError(f"object name already exists in namespace {namespace}: {name}")

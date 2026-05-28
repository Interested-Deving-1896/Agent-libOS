from __future__ import annotations

from dataclasses import replace
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.exceptions import CapabilityDenied, NotFound
from agent_libos.ids import estimate_tokens, new_id, utc_now
from agent_libos.models import (
    EventType,
    MaterializedContext,
    MemoryView,
    MemoryViewSpec,
    MergePolicy,
    MergeResult,
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
    def __init__(
        self,
        store: SQLiteStore,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
    ):
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
    ) -> ObjectHandle:
        now = utc_now()
        obj_type = ObjectType(object_type)
        meta = metadata or ObjectMetadata(token_estimate=estimate_tokens(payload))
        if meta.token_estimate is None:
            meta.token_estimate = estimate_tokens(payload)
        obj = AgentObject(
            oid=new_id("obj"),
            type=obj_type,
            schema_version="1",
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
            payload={"oid": obj.oid, "type": obj.type.value},
        )
        self.audit.record(
            actor=pid,
            action="memory.create_object",
            target=f"object:{obj.oid}",
            output_refs=[obj.oid],
            capability_refs=[handle.capability_id],
        )
        return handle

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

    def update_object(self, pid: str, handle: ObjectHandle, patch: ObjectPatch) -> ObjectHandle:
        self.capabilities.assert_handle(pid, handle, ObjectRight.WRITE)
        current = self.store.get_object(handle.oid)
        if current is None:
            raise NotFound(f"object not found: {handle.oid}")
        if current.immutable:
            raise CapabilityDenied(f"immutable object cannot be updated: {handle.oid}")
        updated = replace(
            current,
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
            payload={"oid": updated.oid, "version": updated.version},
        )
        self.audit.record(
            actor=pid,
            action="memory.update_object",
            target=f"object:{updated.oid}",
            input_refs=[updated.oid],
            output_refs=[updated.oid],
            capability_refs=[handle.capability_id],
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
        for obj in self.store.list_objects():
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
            target="object:*",
            output_refs=[handle.oid for handle in results],
            decision={"count": len(results)},
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

    def materialize_context(
        self,
        pid: str,
        view: MemoryView,
        policy: str = "plan_first",
        budget_tokens: int = 8000,
    ) -> MaterializedContext:
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

        objects = self._sort_for_policy(objects, policy)
        chunks: list[str] = []
        refs: list[str] = []
        total = 0
        for obj in objects:
            tokens = obj.metadata.token_estimate or estimate_tokens(obj.payload)
            if total + tokens > budget_tokens:
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
            policy_used=policy,
        )
        self.audit.record(
            actor=pid,
            action="memory.materialize_context",
            target=f"view:{view.view_id}",
            input_refs=[handle.oid for handle in view.roots],
            output_refs=refs,
            decision={"tokens": total, "omitted": omitted, "policy": policy},
        )
        return context

    def _search_text(self, obj: AgentObject) -> str:
        return " ".join(
            [
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
        return f"[{obj.oid}] type={obj.type.value} version={obj.version}{title}{summary}\npayload: {obj.payload!r}"

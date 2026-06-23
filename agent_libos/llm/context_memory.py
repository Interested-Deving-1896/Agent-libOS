from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from typing import Any

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models.exceptions import ResourceLimitExceeded, ValidationError
from agent_libos.utils.ids import estimate_tokens, utc_now
from agent_libos.models import (
    AgentImage,
    AgentObject,
    AgentProcess,
    Capability,
    Event,
    MaterializedContext,
    MemoryView,
    ObjectHandle,
    ObjectMetadata,
    ObjectPatch,
    ObjectRight,
    ObjectType,
    ResourceUsage,
    ViewMode,
)

_LLM_CONTEXT_DEFAULTS = DEFAULT_CONFIG.llm_context
LLM_CONTEXT_POLICY = _LLM_CONTEXT_DEFAULTS.policy
LLM_CONTEXT_SCHEMA_VERSION = _LLM_CONTEXT_DEFAULTS.schema_version


class LLMContextMemory:
    """Maintains the prompt context as a mutable, append-only Object Memory object."""

    def __init__(self, runtime: Any):
        self.runtime = runtime

    def prepare(
        self,
        pid: str,
        image: AgentImage,
        process: AgentProcess,
        source_context: MaterializedContext,
        events: list[Event],
        capabilities: list[Capability],
        tools: list[dict[str, Any]],
    ) -> MaterializedContext:
        handle = self.ensure(pid, image, process, tools)
        obj = self.runtime.memory.get_object(pid, handle)
        payload = self._payload(obj)
        changed = self._append_deltas(
            payload=payload,
            process=process,
            image=image,
            source_context=source_context,
            events=events,
            capabilities=capabilities,
            tools=tools,
        )
        if changed:
            metadata = ObjectMetadata(
                title=f"LLM context for {pid}",
                summary="Append-only process prompt context optimized for prompt caching.",
                tags=["llm_context", "prompt_cache"],
                token_estimate=estimate_tokens(payload),
            )
            self.runtime.memory.update_object(
                pid,
                handle,
                ObjectPatch(payload=payload, metadata=metadata),
            )
            obj = self.runtime.memory.get_object(pid, handle)
        rendered = self.render(obj.payload)
        token_count = estimate_tokens(rendered)
        self._charge_rendered_context(pid, process, obj.oid, token_count)
        return MaterializedContext(
            text=rendered,
            object_refs=[obj.oid, *source_context.object_refs],
            token_count=token_count,
            omitted_objects=source_context.omitted_objects,
            policy_used=LLM_CONTEXT_POLICY,
        )

    def _charge_rendered_context(self, pid: str, process: AgentProcess, context_oid: str, token_count: int) -> None:
        resources = getattr(self.runtime, "resources", None)
        if resources is None:
            return
        window_limit = resources.context_materialization_window_limit(pid)
        if token_count > window_limit:
            raise ResourceLimitExceeded(
                "llm_context materialization tokens="
                f"{token_count} exceeds max_context_materialization_tokens={window_limit}"
            )
        resources.charge(
            pid,
            ResourceUsage(context_materialized_tokens=token_count),
            source="llm.context_memory",
            context={
                "view_id": process.memory_view.view_id if process.memory_view is not None else None,
                "object_oid": context_oid,
                "policy": LLM_CONTEXT_POLICY,
            },
            allow_overage=False,
            kill_on_exceed=False,
        )

    def ensure(self, pid: str, image: AgentImage, process: AgentProcess, tools: list[dict[str, Any]]) -> ObjectHandle:
        name = context_object_name(pid)
        namespace = self.runtime.memory.resolve_namespace(pid)
        existing = self.runtime.store.get_object_by_name(name, namespace=namespace)
        rights = {
            ObjectRight.READ.value,
            ObjectRight.WRITE.value,
            ObjectRight.MATERIALIZE.value,
            ObjectRight.LINK.value,
            ObjectRight.DIFF.value,
        }
        if existing is None:
            handle = self.runtime.memory.create_object(
                pid=pid,
                object_type=ObjectType.PROCESS_STATE,
                payload=self._initial_payload(pid, image, process, tools),
                metadata=ObjectMetadata(
                    title=f"LLM context for {pid}",
                    summary="Append-only process prompt context optimized for prompt caching.",
                    tags=["llm_context", "prompt_cache"],
                ),
                immutable=False,
                name=name,
            )
        else:
            handle = self.runtime.capability.handle_for_object(
                pid,
                existing.oid,
                rights,
                issued_by="llm.context",
            )
        self._add_handle_to_view(pid, handle)
        return handle

    def view_without_context(self, pid: str, view: MemoryView) -> MemoryView:
        context_oid = self._context_oid(pid)
        if context_oid is None:
            roots = list(view.roots)
        else:
            roots = [handle for handle in view.roots if handle.oid != context_oid]
        return replace(view, roots=roots)

    def render(self, payload: dict[str, Any]) -> str:
        static_prefix = payload.get("static_prefix", {})
        lines = [
            "LLM context object:",
            "Cache strategy: append_only_stable_prefix",
            "The static prefix below should remain stable. New process facts are appended after it.",
            "",
            "Static prefix:",
            _stable_json(static_prefix),
            "",
            "Append-only entries:",
        ]
        for entry in payload.get("entries", []):
            lines.append("")
            lines.append("---")
            lines.append(_stable_json(entry))
        return "\n".join(lines).rstrip()

    def replace_with_compacted_summary(
        self,
        pid: str,
        *,
        context_oid: str,
        expected_version: int,
        summary: dict[str, Any],
        compaction_method: str,
        compaction_metadata: dict[str, Any] | None = None,
        preserve_recent_entries: int,
        source_tokens: int,
        target_tokens: int,
        compressor_pids: list[str],
        source_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically replace LLM context entries with a validated compact summary."""
        obj = self.runtime.store.get_object(context_oid)
        if obj is None and source_payload is None:
            raise ValidationError(f"LLM context object not found: {context_oid}")
        if obj is not None and obj.version != expected_version:
            raise ValidationError(
                "LLM context changed during compaction: "
                f"expected version {expected_version}, found {obj.version}"
            )
        if obj is None:
            current = self.runtime.store.get_object_by_name(
                context_object_name(pid),
                namespace=self.runtime.memory.resolve_namespace(pid),
            )
            if current is not None:
                raise ValidationError(
                    "LLM context changed during compaction: "
                    f"expected missing oid {context_oid}, found {current.oid} version {current.version}"
                )
            payload = self._payload_dict(source_payload)
        else:
            payload = self._payload(obj)
        compacted_payload, compacted_tokens, preserved_count = self._build_compacted_payload(
            context_oid=context_oid,
            expected_version=expected_version,
            payload=payload,
            summary=summary,
            compaction_method=compaction_method,
            compaction_metadata=compaction_metadata,
            preserve_recent_entries=preserve_recent_entries,
            source_tokens=source_tokens,
            target_tokens=target_tokens,
            compressor_pids=compressor_pids,
        )
        metadata = ObjectMetadata(
            title=f"LLM context for {pid}",
            summary="Compacted process prompt context optimized for bounded long-running sessions.",
            tags=["llm_context", "prompt_cache", "compacted", f"compaction_method:{compaction_method}"],
            token_estimate=estimate_tokens(compacted_payload),
        )
        if obj is None:
            handle = self.runtime.memory.create_object(
                pid=pid,
                object_type=ObjectType.PROCESS_STATE,
                payload=compacted_payload,
                metadata=metadata,
                immutable=False,
                name=context_object_name(pid),
            )
            updated_obj = self.runtime.memory.get_object(pid, handle)
        else:
            handle = self.runtime.memory.handle_for_oid(
                pid,
                context_oid,
                required_rights={ObjectRight.READ.value, ObjectRight.WRITE.value},
                optional_rights={ObjectRight.MATERIALIZE.value, ObjectRight.LINK.value, ObjectRight.DIFF.value},
                issued_by="llm.context.compact",
            )
            updated = self.runtime.memory.update_object(
                pid,
                handle,
                ObjectPatch(payload=compacted_payload, metadata=metadata),
            )
            updated_obj = self.runtime.memory.get_object(pid, updated)
        view_handle = self.runtime.capability.handle_for_object(
            pid,
            updated_obj.oid,
            {
                ObjectRight.READ.value,
                ObjectRight.WRITE.value,
                ObjectRight.MATERIALIZE.value,
                ObjectRight.LINK.value,
                ObjectRight.DIFF.value,
            },
            issued_by="llm.context.compact",
        )
        self._add_handle_to_view(pid, view_handle)
        return {
            "context_oid": updated_obj.oid,
            "old_version": expected_version,
            "new_version": updated_obj.version,
            "source_tokens": source_tokens,
            "compacted_tokens": compacted_tokens,
            "preserved_recent_entries": preserved_count,
        }

    def _build_compacted_payload(
        self,
        *,
        context_oid: str,
        expected_version: int,
        payload: dict[str, Any],
        summary: dict[str, Any],
        compaction_method: str,
        compaction_metadata: dict[str, Any] | None,
        preserve_recent_entries: int,
        source_tokens: int,
        target_tokens: int,
        compressor_pids: list[str],
    ) -> tuple[dict[str, Any], int, int]:
        compact_summary = self._validate_compact_summary(summary)
        selected_method = self._validate_compaction_method(compaction_method)
        selected_metadata = self._validate_compaction_metadata(compaction_metadata)
        entries = list(payload.get("entries", []))
        preserved_count = max(0, min(int(preserve_recent_entries), len(entries)))
        preserved_entries = deepcopy(entries[-preserved_count:]) if preserved_count else []
        compacted_payload = deepcopy(payload)
        compact_entry = {
            "kind": "context_compacted",
            "at": utc_now(),
            "source_oid": context_oid,
            "source_version": expected_version,
            "source_entry_count": len(entries),
            "source_tokens": source_tokens,
            "target_tokens": target_tokens,
            "compaction_method": selected_method,
            "compaction_metadata": selected_metadata,
            "compressor_pids": list(compressor_pids),
            "summary": compact_summary,
            "preserved_recent_entries": preserved_count,
        }
        compacted_payload["entries"] = [compact_entry, *preserved_entries]
        compacted_payload["cache_strategy"] = {
            **dict(compacted_payload.get("cache_strategy") or {}),
            "mode": "compacted_stable_prefix",
            "reason": (
                "Older append-only entries were summarized by context compaction; "
                "recent entries remain verbatim."
            ),
            "compacted_at": compact_entry["at"],
        }
        rendered = self.render(compacted_payload)
        compacted_tokens = estimate_tokens(rendered)
        compact_entry["compacted_tokens"] = compacted_tokens
        return compacted_payload, compacted_tokens, preserved_count

    def _validate_compaction_method(self, method: str) -> str:
        if not isinstance(method, str) or not method.strip():
            raise ValidationError("context compaction method must be a non-empty string")
        selected = method.strip()
        if len(selected) > 128:
            raise ValidationError("context compaction method is too long")
        return selected

    def _validate_compaction_metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        if metadata is None:
            return {}
        if not isinstance(metadata, dict):
            raise ValidationError("context compaction metadata must be a JSON object")
        return deepcopy(metadata)

    def _validate_compact_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(summary, dict):
            raise ValidationError("context compaction summary must be a JSON object")
        required = {
            "goal",
            "constraints",
            "user_preferences",
            "completed",
            "pending",
            "key_references",
            "recent_decisions",
            "risks",
            "uncertainties",
            "next_steps",
        }
        missing = sorted(required - set(summary))
        if missing:
            raise ValidationError(f"context compaction summary missing fields: {missing}")
        if not any(summary.get(key) for key in required):
            raise ValidationError("context compaction summary is empty")
        return deepcopy(summary)

    def _initial_payload(
        self,
        pid: str,
        image: AgentImage,
        process: AgentProcess,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "kind": "llm_context",
            "schema_version": LLM_CONTEXT_SCHEMA_VERSION,
            "cache_strategy": {
                "mode": "append_only_stable_prefix",
                "reason": "Keep repeated instructions, tool names, and early process context at the front; append changes at the end.",
            },
            "static_prefix": {
                "pid": pid,
                "image_id": image.image_id,
                "image_name": image.name,
                "image_version": image.version,
                "safety_profile": image.safety_profile,
                "context_policy": image.context_policy,
                "parent_pid": process.parent_pid,
                "initial_working_directory": process.working_directory,
                "goal_oid": process.goal_oid,
                "tool_names": sorted(_tool_name(tool) for tool in tools if _tool_name(tool)),
            },
            "entries": [
                {
                    "kind": "process_started",
                    "at": process.created_at,
                    "pid": pid,
                    "parent_pid": process.parent_pid,
                    "goal_oid": process.goal_oid,
                    "working_directory": process.working_directory,
                }
            ],
            "captured": {
                "object_oids": [],
                "objects": {},
                "event_ids": [],
                "capability_signature": None,
                "tool_signature": _tool_signature(tools),
                "process_signature": None,
            },
        }

    def _append_deltas(
        self,
        payload: dict[str, Any],
        process: AgentProcess,
        image: AgentImage,
        source_context: MaterializedContext,
        events: list[Event],
        capabilities: list[Capability],
        tools: list[dict[str, Any]],
    ) -> bool:
        changed = False
        captured = payload.setdefault("captured", {})
        entries = payload.setdefault("entries", [])

        process_signature = {
            "status": process.status.value,
            "status_message": process.status_message,
            "checkpoint_head": process.checkpoint_head,
            "image_id": image.image_id,
            "working_directory": process.working_directory,
        }
        if captured.get("process_signature") != process_signature:
            entries.append({"kind": "process_snapshot", "at": utc_now(), **process_signature})
            captured["process_signature"] = process_signature
            changed = True

        capability_signature = _capability_signature(capabilities)
        if captured.get("capability_signature") != capability_signature:
            entries.append({"kind": "capabilities_snapshot", "at": utc_now(), "capabilities": capability_signature})
            captured["capability_signature"] = capability_signature
            changed = True

        tool_signature = _tool_signature(tools)
        if captured.get("tool_signature") != tool_signature:
            entries.append({"kind": "tool_table_snapshot", "at": utc_now(), "tools": tool_signature})
            captured["tool_signature"] = tool_signature
            changed = True

        captured_events = set(captured.get("event_ids", []))
        new_events = [
            event for event in events[-_LLM_CONTEXT_DEFAULTS.recent_event_limit :] if event.event_id not in captured_events
        ]
        if new_events:
            entries.append(
                {
                    "kind": "events_delta",
                    "at": utc_now(),
                    "events": [
                        {
                            "event_id": event.event_id,
                            "type": event.type.value,
                            "source": event.source,
                            "target": event.target,
                            "payload": event.payload,
                        }
                        for event in new_events
                    ],
                }
            )
            captured["event_ids"] = sorted(captured_events | {event.event_id for event in new_events})
            changed = True

        captured_objects = _captured_object_signatures(captured)
        changed_oids: list[str] = []
        next_captured_objects = dict(captured_objects)
        for oid in source_context.object_refs:
            signature = self._object_signature(oid)
            if captured_objects.get(oid) != signature:
                changed_oids.append(oid)
            next_captured_objects[oid] = signature
        if changed_oids:
            entries.append(
                {
                    "kind": "memory_delta",
                    "at": utc_now(),
                    "policy": source_context.policy_used,
                    "token_estimate": source_context.token_count,
                    "omitted_objects": list(source_context.omitted_objects),
                    "objects": [self._object_entry(oid) for oid in changed_oids],
                }
            )
            captured["objects"] = dict(sorted(next_captured_objects.items()))
            captured["object_oids"] = sorted(next_captured_objects)
            changed = True

        if source_context.omitted_objects:
            omitted = sorted(set(source_context.omitted_objects))
            if captured.get("omitted_objects") != omitted:
                entries.append({"kind": "context_omissions", "at": utc_now(), "omitted_objects": omitted})
                captured["omitted_objects"] = omitted
                changed = True

        return changed

    def _object_entry(self, oid: str) -> dict[str, Any]:
        obj = self.runtime.store.get_object(oid)
        if obj is None:
            return {"oid": oid, "missing": True}
        return {
            "oid": obj.oid,
            "name": obj.name,
            "type": obj.type.value,
            "version": obj.version,
            "title": obj.metadata.title,
            "summary": obj.metadata.summary,
            "payload": obj.payload,
        }

    def _object_signature(self, oid: str) -> dict[str, Any]:
        obj = self.runtime.store.get_object(oid)
        if obj is None:
            return {"missing": True}
        return {"version": obj.version, "updated_at": obj.updated_at}

    def _payload(self, obj: AgentObject) -> dict[str, Any]:
        return self._payload_dict(obj.payload, label=obj.name)

    def _payload_dict(self, payload: Any, *, label: str = "payload") -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("kind") != "llm_context":
            raise ValidationError(f"object is not an LLM context object: {label}")
        return payload

    def _context_oid(self, pid: str) -> str | None:
        obj = self.runtime.store.get_object_by_name(
            context_object_name(pid),
            namespace=self.runtime.memory.resolve_namespace(pid),
        )
        return obj.oid if obj is not None else None

    def _add_handle_to_view(self, pid: str, handle: ObjectHandle) -> None:
        process = self.runtime.process.get(pid)
        if process.memory_view is None:
            process.memory_view = self.runtime.memory.create_view(pid, [handle], mode=ViewMode.MUTABLE)
        elif all(existing.oid != handle.oid for existing in process.memory_view.roots):
            process.memory_view.roots.insert(0, handle)
        else:
            process.memory_view.roots = [
                handle if existing.oid == handle.oid and "write" not in existing.rights else existing
                for existing in process.memory_view.roots
            ]
        process.updated_at = utc_now()
        self.runtime.store.update_process(process)


def context_object_name(pid: str) -> str:
    return f"{_LLM_CONTEXT_DEFAULTS.object_name_prefix}:{pid}"


def _tool_name(tool: dict[str, Any]) -> str | None:
    spec_json = tool.get("spec_json")
    if isinstance(spec_json, str):
        try:
            spec = json.loads(spec_json)
            if isinstance(spec, dict):
                return str(spec.get("name") or tool.get("name") or "")
        except json.JSONDecodeError:
            pass
    return str(tool.get("name") or "") or None


def _tool_signature(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        spec_json = tool.get("spec_json")
        spec = {}
        if isinstance(spec_json, str):
            try:
                decoded = json.loads(spec_json)
                if isinstance(decoded, dict):
                    spec = decoded
            except json.JSONDecodeError:
                spec = {}
        name = str(spec.get("name") or tool.get("name") or "")
        if not name:
            continue
        result.append(
            {
                "name": name,
                "description": spec.get("description", ""),
                "tags": spec.get("tags", []),
                "policy": spec.get("policy", {}),
                "input_schema": spec.get("input_schema", {}),
            }
        )
    return sorted(result, key=lambda item: item["name"])


def _capability_signature(capabilities: list[Capability]) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "cap_id": cap.cap_id,
                "resource": cap.resource,
                "rights": sorted(cap.rights),
                "effect": cap.effect.value,
                "status": cap.status.value,
                "policy": _capability_policy(cap),
                "uses_remaining": cap.uses_remaining,
                "delegable": cap.delegable,
                "delegation_depth": cap.delegation_depth,
                "issuer": cap.issued_by,
                "parent_cap_id": cap.parent_cap_id,
                "expires_at": cap.expires_at,
            }
            for cap in capabilities
            if cap.active
        ],
        key=lambda item: (item["resource"], ",".join(item["rights"]), item["cap_id"]),
    )


def _capability_policy(cap: Capability) -> str:
    if cap.effect.value == "allow":
        return "allow_once" if cap.uses_remaining is not None else "always_allow"
    if cap.effect.value == "deny":
        return "always_deny"
    if cap.effect.value == "ask":
        return "ask_each_time"
    return cap.effect.value


def _captured_object_signatures(captured: dict[str, Any]) -> dict[str, dict[str, Any]]:
    objects = captured.get("objects")
    if isinstance(objects, dict):
        return {str(oid): dict(signature) for oid, signature in objects.items() if isinstance(signature, dict)}
    return {str(oid): {"legacy_captured": True} for oid in captured.get("object_oids", [])}


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str)

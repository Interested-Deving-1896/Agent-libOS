from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from agent_libos.config import DEFAULT_CONFIG
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
        return MaterializedContext(
            text=rendered,
            object_refs=[obj.oid, *source_context.object_refs],
            token_count=estimate_tokens(rendered),
            omitted_objects=source_context.omitted_objects,
            policy_used=LLM_CONTEXT_POLICY,
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

        captured_oids = set(captured.get("object_oids", []))
        new_oids = [oid for oid in source_context.object_refs if oid not in captured_oids]
        if new_oids:
            entries.append(
                {
                    "kind": "memory_delta",
                    "at": utc_now(),
                    "policy": source_context.policy_used,
                    "token_estimate": source_context.token_count,
                    "omitted_objects": list(source_context.omitted_objects),
                    "objects": [self._object_entry(oid) for oid in new_oids],
                }
            )
            captured["object_oids"] = sorted(captured_oids | set(new_oids))
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

    def _payload(self, obj: AgentObject) -> dict[str, Any]:
        if not isinstance(obj.payload, dict) or obj.payload.get("kind") != "llm_context":
            raise ValueError(f"object is not an LLM context object: {obj.name}")
        return obj.payload

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
                "resource": cap.resource,
                "rights": sorted(cap.rights),
                "permission_policy": cap.constraints.get("permission_policy", "always_allow"),
                "expires_at": cap.expires_at,
            }
            for cap in capabilities
            if not cap.revoked
        ],
        key=lambda item: (item["resource"], ",".join(item["rights"])),
    )


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str)

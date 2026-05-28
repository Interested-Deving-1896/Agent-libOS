from __future__ import annotations

import builtins
from dataclasses import dataclass, field

from agent_libos.exceptions import NotFound


@dataclass(frozen=True)
class ToolBundle:
    bundle_id: str
    name: str
    tool_names: list[str] = field(default_factory=list)
    skill_ids: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class ToolBundleManager:
    def __init__(self):
        self._bundles: dict[str, ToolBundle] = {}

    def register(self, bundle: ToolBundle) -> ToolBundle:
        self._bundles[bundle.bundle_id] = bundle
        return bundle

    def get(self, bundle_id: str) -> ToolBundle:
        try:
            return self._bundles[bundle_id]
        except KeyError as exc:
            raise NotFound(f"tool bundle not found: {bundle_id}") from exc

    def list(self) -> builtins.list[ToolBundle]:
        return builtins.list(self._bundles.values())

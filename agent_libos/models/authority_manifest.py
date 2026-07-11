from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TaskAuthorityManifest:
    """Host-authored launch authority contract for one process.

    Image capability declarations are copied into ``required_capabilities`` for
    comparison only.  Authority is issued exclusively from
    ``authorized_capabilities``.
    """

    manifest_id: str
    pid: str
    image_id: str
    goal_ref: str | None
    authorized_capabilities: list[dict[str, Any]] = field(default_factory=list)
    required_capabilities: list[dict[str, Any]] = field(default_factory=list)
    permitted_effects: list[str] = field(default_factory=list)
    resource_budget: dict[str, Any] = field(default_factory=dict)
    approval_policy: dict[str, Any] = field(default_factory=dict)
    data_flow_policy: dict[str, Any] = field(default_factory=dict)
    expires_at: str | None = None
    issued_by: str = "runtime.bootstrap"
    parent_manifest_id: str | None = None
    manifest_hash: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @property
    def missing_required_capabilities(self) -> list[dict[str, Any]]:
        authorized = {
            (
                str(item.get("resource") or ""),
                tuple(sorted(str(right) for right in item.get("rights", []))),
            )
            for item in self.authorized_capabilities
        }
        return [
            dict(item)
            for item in self.required_capabilities
            if (
                str(item.get("resource") or ""),
                tuple(sorted(str(right) for right in item.get("rights", []))),
            )
            not in authorized
        ]

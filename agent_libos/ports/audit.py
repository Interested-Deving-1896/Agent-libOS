from __future__ import annotations

from typing import Any, Protocol

from agent_libos.models import AuditRecord


class AuditPort(Protocol):
    """Minimal audit sink consumed by core services."""

    def record(
        self,
        actor: str,
        action: str,
        target: str | None = None,
        input_refs: list[str] | None = None,
        output_refs: list[str] | None = None,
        capability_refs: list[str] | None = None,
        decision: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        parent_record_id: str | None = None,
    ) -> AuditRecord:
        ...

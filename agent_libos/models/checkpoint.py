from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_libos.models.base import CheckpointID, PID


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: CheckpointID
    pid: PID
    reason: str
    created_at: str
    created_by: str | None = None
    snapshot_version: int = 1
    metadata: dict[str, Any] | None = None

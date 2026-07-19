from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_libos.models.base import CheckpointID, PID


CHECKPOINT_SNAPSHOT_VERSION = 4


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: CheckpointID
    pid: PID
    reason: str
    created_at: str
    created_by: str | None = None
    snapshot_version: int = CHECKPOINT_SNAPSHOT_VERSION
    metadata: dict[str, Any] | None = None
    effect_ledger_seq: int = 0

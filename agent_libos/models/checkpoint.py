from __future__ import annotations

from dataclasses import dataclass

from agent_libos.models.base import CheckpointID, PID


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: CheckpointID
    pid: PID
    reason: str
    created_at: str

from __future__ import annotations

from agent_libos.ids import new_id, utc_now
from agent_libos.models import Checkpoint, EventType
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.storage import SQLiteStore


class CheckpointManager:
    def __init__(self, store: SQLiteStore, audit: AuditManager, events: EventBus):
        self.store = store
        self.audit = audit
        self.events = events

    def checkpoint(self, pid: str, reason: str) -> str:
        checkpoint = Checkpoint(
            checkpoint_id=new_id("ckpt"),
            pid=pid,
            reason=reason,
            created_at=utc_now(),
        )
        snapshot = self.store.snapshot_tables()
        self.store.insert_checkpoint(checkpoint, snapshot)
        process = self.store.get_process(pid)
        if process is not None:
            process.checkpoint_head = checkpoint.checkpoint_id
            process.updated_at = utc_now()
            self.store.update_process(process)
        self.events.emit(
            EventType.CHECKPOINT_CREATED,
            source=pid,
            target=pid,
            payload={"checkpoint_id": checkpoint.checkpoint_id, "reason": reason},
        )
        self.audit.record(
            actor=pid,
            action="checkpoint.create",
            target=f"checkpoint:{checkpoint.checkpoint_id}",
            decision={"reason": reason},
        )
        return checkpoint.checkpoint_id

    def rollback(self, pid: str, checkpoint_id: str) -> dict[str, str]:
        found = self.store.get_checkpoint_snapshot(checkpoint_id)
        if found is None:
            raise KeyError(f"checkpoint not found: {checkpoint_id}")
        checkpoint, snapshot = found
        self.store.restore_tables(snapshot)
        self.events.emit(
            EventType.ROLLBACK,
            source=pid,
            target=checkpoint.pid,
            payload={"checkpoint_id": checkpoint_id},
        )
        self.audit.record(
            actor=pid,
            action="checkpoint.rollback",
            target=f"checkpoint:{checkpoint_id}",
            decision={"restored_for": checkpoint.pid},
        )
        return {"checkpoint_id": checkpoint_id, "pid": checkpoint.pid, "status": "rolled_back"}


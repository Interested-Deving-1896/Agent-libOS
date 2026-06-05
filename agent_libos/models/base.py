from __future__ import annotations

from enum import Enum

PID = str
OID = str
CapabilityID = str
EventID = str
AuditID = str
CheckpointID = str
ToolID = str
HumanRequestID = str
ProcessMessageID = str
MemoryViewID = str
SnapshotID = str
NamespaceID = str


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value

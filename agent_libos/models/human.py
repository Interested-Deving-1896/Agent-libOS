from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_libos.models.base import HumanRequestID, PID, StrEnum


class HumanRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"
    CANCELLED = "cancelled"
    DELIVERED = "delivered"


@dataclass
class HumanRequest:
    request_id: HumanRequestID
    pid: PID
    human: str
    payload: dict[str, Any]
    status: HumanRequestStatus
    decision: dict[str, Any] | None
    blocking: bool
    created_at: str
    updated_at: str

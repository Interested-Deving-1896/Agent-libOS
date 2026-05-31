from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_libos.models.base import CapabilityID, StrEnum


class CapabilityRight(StrEnum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    LINK = "link"
    DIFF = "diff"
    MATERIALIZE = "materialize"
    DELETE = "delete"
    GRANT = "grant"
    REVOKE = "revoke"
    APPROVE = "approve"
    ADMIN = "admin"


@dataclass(frozen=True)
class Capability:
    cap_id: CapabilityID
    subject: str
    resource: str
    rights: set[str]
    constraints: dict[str, Any]
    issued_by: str
    issued_at: str
    expires_at: str | None = None
    delegable: bool = False
    revocable: bool = True
    revoked: bool = False

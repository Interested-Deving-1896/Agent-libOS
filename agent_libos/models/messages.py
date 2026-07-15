from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_libos.models.base import PID, ProcessMessageID, StrEnum


class ProcessMessageKind(StrEnum):
    NORMAL = "normal"
    INTERRUPT = "interrupt"


class ProcessMessageStatus(StrEnum):
    UNREAD = "unread"
    ACKED = "acked"
    SUPERSEDED_BY_RESTORE = "superseded_by_restore"


@dataclass
class ProcessMessage:
    message_id: ProcessMessageID
    sender: str
    recipient_pid: PID
    kind: ProcessMessageKind
    subject: str
    body: str
    channel: str = "default"
    correlation_id: str | None = None
    reply_to: ProcessMessageID | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    status: ProcessMessageStatus = ProcessMessageStatus.UNREAD
    created_at: str = ""
    updated_at: str = ""
    acked_at: str | None = None
    # Keep new envelope metadata after every legacy field. ProcessMessage is a
    # public dataclass and older callers may still pass status/timestamps by
    # position.
    metadata: dict[str, Any] = field(default_factory=dict)


def conservative_legacy_process_message_metadata(
    value: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify messages whose historical flow carrier is unavailable.

    Pre-data-flow stores and checkpoints persisted no trusted message labels.
    Treating that absence as NORMAL would let unread legacy payloads enter a
    process without taint, so migration uses the most restrictive labels.
    """

    labels: dict[str, str | None] = {
        "sensitivity": "secret",
        "trust_level": "untrusted",
        "integrity": "untrusted",
        "origin": "legacy",
        "tenant": None,
        "principal": None,
        "declassification_authority": None,
    }
    selected = dict(value or {})
    # A carrier created under incomplete historical labels is not trusted to
    # represent the conservative migration result. Observation must create a
    # fresh carrier from the labels below.
    selected.pop("label_carrier_oid", None)
    selected.update({
        "source_oids": [],
        "data_labels": dict(labels),
        "data_flow_context": {
            "labels": dict(labels),
            "source_refs": [],
            "materialization_id": None,
        },
    })
    return selected

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, cast

from agent_libos.models.base import StrEnum


class RuntimePublicationKind(StrEnum):
    PROCESS_LAUNCH = "process_launch"
    PROCESS_EXEC = "process_exec"
    CHECKPOINT_RESTORE = "checkpoint_restore"


def parse_runtime_publication_kind(value: object) -> RuntimePublicationKind:
    """Accept only an exact publication enum or one of its raw string values."""

    if isinstance(value, RuntimePublicationKind):
        return value
    if type(value) is not str:
        raise ValueError("runtime publication kind must be an exact string value")
    try:
        return RuntimePublicationKind(value)
    except ValueError as exc:
        raise ValueError(f"invalid runtime publication kind: {value!r}") from exc


RuntimePublicationState = Literal[
    "planning",
    "applying",
    "reconciliation_pending",
    "committed",
    "rollback_pending",
    "rolled_back",
    "failed",
    "manual",
]

RUNTIME_PUBLICATION_STATES = frozenset(
    {
        "planning",
        "applying",
        "reconciliation_pending",
        "committed",
        "rollback_pending",
        "rolled_back",
        "failed",
        "manual",
    }
)


def parse_runtime_publication_state(value: object) -> RuntimePublicationState:
    """Reject non-canonical publication states before they reach storage."""

    if type(value) is not str or value not in RUNTIME_PUBLICATION_STATES:
        raise ValueError(f"invalid runtime publication state: {value!r}")
    return cast(RuntimePublicationState, value)


def _publication_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"runtime publication {field_name} must be non-empty text")
    return value


PayloadDeliveryState = Literal["pending", "confirmed", "completed"]
PAYLOAD_DELIVERY_STATES = frozenset({"pending", "confirmed", "completed"})


class CheckpointPayloadDeliveryAttemptState(StrEnum):
    """Durable control state for one exact checkpoint payload attempt."""

    PREPARING = "preparing"
    ACKED = "acked"
    ABORTED = "aborted"


@dataclass(frozen=True, order=True, slots=True)
class CheckpointPayloadDeliveryAttempt:
    """Durable scalar token identifying one startup delivery attempt."""

    started_at: str
    attempt_id: str
    owner_instance_id: str

    def __post_init__(self) -> None:
        _publication_text(self.started_at, "payload delivery started_at")
        _publication_text(self.attempt_id, "payload delivery attempt_id")
        _publication_text(
            self.owner_instance_id,
            "payload delivery owner_instance_id",
        )


@dataclass(frozen=True, slots=True)
class CheckpointPayloadDeliveryAttemptPage:
    """One hard-bounded page of unfinished startup delivery attempts."""

    records: tuple[CheckpointPayloadDeliveryAttempt, ...]
    next_cursor: CheckpointPayloadDeliveryAttempt | None = None

    def __post_init__(self) -> None:
        if self.next_cursor is not None and not isinstance(
            self.next_cursor,
            CheckpointPayloadDeliveryAttempt,
        ):
            raise ValueError("payload delivery attempt cursor has an invalid type")
        if self.next_cursor is not None and not self.records:
            raise ValueError("empty payload delivery attempt page cannot have a cursor")


@dataclass(frozen=True, order=True, slots=True)
class RuntimePublicationCursor:
    """Stable keyset cursor for bounded publication reconciliation."""

    created_at: str
    publication_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.created_at, str) or not self.created_at:
            raise ValueError("runtime publication cursor created_at must not be empty")
        if not isinstance(self.publication_id, str) or not self.publication_id:
            raise ValueError("runtime publication cursor publication_id must not be empty")


class RuntimePublicationRecord(TypedDict):
    """Stable persistence shape for an in-flight runtime publication."""

    publication_id: str
    kind: RuntimePublicationKind
    pid: str
    owner_instance_id: str
    state: RuntimePublicationState
    phase: str
    plan: dict[str, Any]
    receipt: dict[str, Any]
    error: dict[str, Any] | None
    operation_reconciled: bool
    payload_delivery_state: PayloadDeliveryState | None
    payload_delivery_attempt_id: str | None
    payload_delivery_started_at: str | None
    created_at: str
    updated_at: str


def _payload_delivery_projection(
    value: Mapping[str, object],
) -> tuple[PayloadDeliveryState | None, str | None, str | None]:
    delivery_state = value.get("payload_delivery_state")
    if delivery_state is not None and (
        type(delivery_state) is not str
        or delivery_state not in PAYLOAD_DELIVERY_STATES
    ):
        raise ValueError("runtime publication payload delivery state is invalid")
    attempt_id = value.get("payload_delivery_attempt_id")
    started_at = value.get("payload_delivery_started_at")
    if attempt_id is not None:
        attempt_id = _publication_text(attempt_id, "payload delivery attempt_id")
    if started_at is not None:
        started_at = _publication_text(started_at, "payload delivery started_at")
    if (attempt_id is None) is not (started_at is None):
        raise ValueError(
            "runtime publication payload delivery attempt projection is incomplete"
        )
    return cast(PayloadDeliveryState | None, delivery_state), attempt_id, started_at


def _validate_payload_delivery_projection(
    *,
    receipt: Mapping[str, object],
    kind: RuntimePublicationKind,
    state: RuntimePublicationState,
    phase: str,
    delivery_state: PayloadDeliveryState | None,
    attempt_id: str | None,
    started_at: str | None,
) -> None:
    receipt_delivery = receipt.get("payload_delivery")
    receipt_attempt = receipt.get("payload_delivery_attempt")
    if delivery_state is None:
        if (
            receipt_delivery is not None
            or receipt_attempt is not None
            or attempt_id is not None
            or started_at is not None
        ):
            raise ValueError(
                "runtime publication payload delivery receipt lacks its projection"
            )
        return
    if receipt_delivery != {"state": delivery_state}:
        raise ValueError(
            "runtime publication payload delivery receipt and projection differ"
        )
    if kind is not RuntimePublicationKind.CHECKPOINT_RESTORE or (
        state != "committed" or phase != "reconciled"
    ):
        raise ValueError(
            "runtime publication payload delivery belongs to an invalid publication"
        )
    if attempt_id is None:
        if delivery_state != "pending" or receipt_attempt is not None:
            raise ValueError("runtime publication payload delivery attempt is missing")
        return
    if delivery_state == "pending":
        raise ValueError("pending payload delivery cannot retain an attempt")
    if receipt_attempt != {"attempt_id": attempt_id, "started_at": started_at}:
        raise ValueError(
            "runtime publication payload delivery attempt receipt and projection differ"
        )


def validate_runtime_publication_record(
    value: Mapping[str, object],
) -> RuntimePublicationRecord:
    """Decode the canonical durable publication shape without coercion."""

    if not isinstance(value, Mapping):
        raise ValueError("runtime publication record must be a mapping")
    plan = value.get("plan")
    receipt = value.get("receipt")
    error = value.get("error")
    if not isinstance(plan, Mapping):
        raise ValueError("runtime publication plan must be a mapping")
    if not isinstance(receipt, Mapping):
        raise ValueError("runtime publication receipt must be a mapping")
    phases = receipt.get("phases")
    artifacts = receipt.get("artifacts")
    if not isinstance(phases, list) or not isinstance(artifacts, list):
        raise ValueError(
            "runtime publication receipt requires phases and artifacts lists"
        )
    if error is not None and not isinstance(error, Mapping):
        raise ValueError("runtime publication error must be a mapping or null")
    operation_reconciled = value.get("operation_reconciled")
    if type(operation_reconciled) is not bool:
        raise ValueError("runtime publication operation_reconciled must be boolean")
    kind = parse_runtime_publication_kind(value.get("kind"))
    state = parse_runtime_publication_state(value.get("state"))
    phase = _publication_text(value.get("phase"), "phase")
    delivery_state, attempt_id, started_at = _payload_delivery_projection(value)
    _validate_payload_delivery_projection(
        receipt=receipt,
        kind=kind,
        state=state,
        phase=phase,
        delivery_state=delivery_state,
        attempt_id=attempt_id,
        started_at=started_at,
    )
    return RuntimePublicationRecord(
        publication_id=_publication_text(
            value.get("publication_id"), "publication_id"
        ),
        kind=kind,
        pid=_publication_text(value.get("pid"), "pid"),
        owner_instance_id=_publication_text(
            value.get("owner_instance_id"), "owner_instance_id"
        ),
        state=state,
        phase=phase,
        plan=dict(plan),
        receipt=dict(receipt),
        error=dict(error) if error is not None else None,
        operation_reconciled=operation_reconciled,
        payload_delivery_state=delivery_state,
        payload_delivery_attempt_id=cast(str | None, attempt_id),
        payload_delivery_started_at=cast(str | None, started_at),
        created_at=_publication_text(value.get("created_at"), "created_at"),
        updated_at=_publication_text(value.get("updated_at"), "updated_at"),
    )


@dataclass(frozen=True, slots=True)
class RuntimePublicationPage:
    """One hard-bounded page of runtime publications."""

    records: tuple[RuntimePublicationRecord, ...]
    next_cursor: RuntimePublicationCursor | None = None

    def __post_init__(self) -> None:
        if self.next_cursor is not None and not isinstance(
            self.next_cursor,
            RuntimePublicationCursor,
        ):
            raise ValueError("runtime publication next_cursor has an invalid type")
        if self.next_cursor is not None and not self.records:
            raise ValueError("empty runtime publication page cannot have a cursor")

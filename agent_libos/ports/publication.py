from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol

from agent_libos.models import (
    CheckpointPayloadDeliveryAttempt,
    CheckpointPayloadDeliveryAttemptPage,
    CheckpointPayloadDeliveryAttemptState,
    PayloadDeliveryState,
    RuntimePublicationCursor,
    RuntimePublicationKind,
    RuntimePublicationPage,
    RuntimePublicationRecord,
    RuntimePublicationState,
)


class RuntimePublicationReceiptRecorder(Protocol):
    """Typed sink for durable, exact runtime-publication artifacts."""

    def record_runtime_publication_artifact(
        self,
        publication_id: str,
        artifact: Mapping[str, Any],
        *,
        expected_states: Iterable[str] | None = None,
    ) -> bool:
        """Append an idempotent artifact receipt to an active publication.

        ``expected_states=None`` disables the state predicate; an explicitly
        empty iterable matches no state and must return ``False``.
        """


class CheckpointRestorePublicationReader(Protocol):
    """Exact read/UoW surface used by checkpoint restore reconciliation."""

    def transaction(self) -> AbstractContextManager[Any]: ...

    def get_runtime_publication(
        self,
        publication_id: str,
    ) -> RuntimePublicationRecord | None: ...

    def query_runtime_publication_operation_reconciliation(
        self,
        *,
        kind: RuntimePublicationKind | str,
        state: RuntimePublicationState | str,
        after: RuntimePublicationCursor | None,
        limit: int,
    ) -> RuntimePublicationPage: ...

    def query_runtime_publication_recovery(
        self,
        *,
        kind: RuntimePublicationKind | str,
        state: RuntimePublicationState | str,
        operation_reconciled: bool,
        after: RuntimePublicationCursor | None,
        limit: int,
    ) -> RuntimePublicationPage: ...

    def query_checkpoint_payload_delivery_attempts(
        self,
        *,
        after: CheckpointPayloadDeliveryAttempt | None,
        limit: int,
    ) -> CheckpointPayloadDeliveryAttemptPage: ...

    def get_checkpoint_payload_delivery_attempt_state(
        self,
        attempt: CheckpointPayloadDeliveryAttempt,
    ) -> CheckpointPayloadDeliveryAttemptState | None: ...

    def query_checkpoint_restore_payload_deliveries(
        self,
        *,
        delivery_state: PayloadDeliveryState | str,
        attempt_id: str | None,
        after: RuntimePublicationCursor | None,
        limit: int,
    ) -> RuntimePublicationPage: ...


class CheckpointRestorePublicationWriterPort(Protocol):
    """Opaque typed mutation surface for checkpoint restore publications."""

    def insert_runtime_publication(
        self,
        *,
        publication_id: str,
        kind: RuntimePublicationKind | str,
        pid: str,
        owner_instance_id: str,
        plan: Mapping[str, Any],
        phase: str = "planned",
    ) -> RuntimePublicationRecord: ...

    def claim_runtime_publication_recovery(
        self,
        publication_id: str,
        *,
        claimant_instance_id: str,
        expected_owner_instance_id: str,
        expected_state: RuntimePublicationState | str,
        classification: str,
        max_attempts: int | None = None,
        allow_orphaned_claim_takeover: bool = False,
        claimed_state: RuntimePublicationState | str = "rollback_pending",
    ) -> RuntimePublicationRecord | None: ...

    def advance_runtime_publication(
        self,
        publication_id: str,
        *,
        state: RuntimePublicationState | str,
        phase: str,
        receipt: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        expected_states: Iterable[RuntimePublicationState | str] | None = None,
        expected_phase: str | None = None,
        recovery_lease_id: str | None = None,
    ) -> bool:
        """CAS one transition; an empty expected-state set rejects all states."""

        ...

    def record_runtime_publication_artifact(
        self,
        publication_id: str,
        artifact: Mapping[str, Any],
        *,
        expected_states: Iterable[RuntimePublicationState | str] | None = None,
    ) -> bool:
        """CAS one receipt append; an empty expected-state set rejects all states."""

        ...

    def mark_runtime_publication_operation_reconciled(
        self,
        publication_id: str,
        *,
        expected_kind: RuntimePublicationKind | str,
        expected_state: RuntimePublicationState | str,
        expected_phase: str,
        expected_operation_id: str | None,
    ) -> bool: ...

    def transition_payload_delivery(
        self,
        publication_id: str,
        *,
        expected_delivery_state: str | None,
        delivery_state: str,
        expected_attempt: CheckpointPayloadDeliveryAttempt | None = None,
        delivery_attempt: CheckpointPayloadDeliveryAttempt | None = None,
        owner_instance_id: str | None = None,
        recovery_lease_id: str | None = None,
    ) -> bool: ...

    def begin_checkpoint_payload_delivery_attempt(
        self,
        attempt: CheckpointPayloadDeliveryAttempt,
    ) -> bool: ...

    def ack_checkpoint_payload_delivery_attempt(
        self,
        attempt: CheckpointPayloadDeliveryAttempt,
    ) -> bool: ...

    def abort_checkpoint_payload_delivery_attempt(
        self,
        attempt: CheckpointPayloadDeliveryAttempt,
    ) -> bool: ...

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, TypeVar

from agent_libos.runtime.snapshots.codec import SnapshotCodec
from agent_libos.runtime.snapshots.models import ProcessSnapshot
from agent_libos.storage import RuntimeStore


PreparedT = TypeVar("PreparedT")
PublishedT = TypeVar("PublishedT")
ReservationT = TypeVar("ReservationT")


class SnapshotCoordinator:
    """Coordinates typed snapshot capture and atomic state publication.

    The manager supplies domain-specific discovery and row mutation callbacks;
    this service owns the invariant-sensitive ordering around validation,
    authority reservation settlement, the store transaction, and compensation.
    """

    def __init__(self, store: RuntimeStore, *, codec: type[SnapshotCodec] = SnapshotCodec):
        self._store = store
        self._codec = codec

    def decode(self, snapshot: Mapping[str, Any]) -> ProcessSnapshot:
        return self._codec.decode_mapping(snapshot)

    def encode(self, snapshot: ProcessSnapshot) -> dict[str, Any]:
        return self._codec.encode_mapping(snapshot)

    def normalize(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        _typed, normalized = self.canonicalize(snapshot)
        return normalized

    def canonicalize(
        self,
        snapshot: Mapping[str, Any],
    ) -> tuple[ProcessSnapshot, dict[str, Any]]:
        return self._codec.canonicalize_mapping(snapshot)

    @contextmanager
    def capture_scope(self) -> Iterator[None]:
        """Hold the one transaction used by discovery and checkpoint publish."""
        with self._store.transaction(include_object_payloads=True):
            yield

    @contextmanager
    def restore_runtime_scope(
        self,
        runtime_quiescence: Callable[[], Any],
    ) -> Iterator[None]:
        """Keep scheduler quanta stopped through post-commit finalizers."""
        with runtime_quiescence():
            yield

    @contextmanager
    def restore_registry_scope(
        self,
        registry_quiescence: Callable[[], Any],
    ) -> Iterator[None]:
        """Hold the registry lock around atomic publish and reconciliation."""
        with registry_quiescence():
            yield

    @contextmanager
    def restore_atomic_scope(
        self,
        ownership_quiescence: Callable[[], Any],
    ) -> Iterator[None]:
        """Acquire ownership before the store lock for restore preflight."""
        with ownership_quiescence():
            with self._store.locked():
                yield

    def atomic_publish(
        self,
        snapshot: Mapping[str, Any] | ProcessSnapshot,
        *,
        reserve: Callable[[], ReservationT],
        prepare: Callable[[ProcessSnapshot], PreparedT],
        settle: Callable[[ReservationT], None],
        publish: Callable[[ProcessSnapshot, PreparedT], PublishedT],
        compensate: Callable[[ReservationT], None],
    ) -> tuple[PreparedT, PublishedT]:
        """Prepare and publish once, restoring reservations on any failure."""
        typed = (
            snapshot
            if isinstance(snapshot, ProcessSnapshot)
            else self.decode(snapshot)
        )
        reservation = reserve()
        try:
            prepared = prepare(typed)
            with self._store.transaction(include_object_payloads=True):
                settle(reservation)
                published = publish(typed, prepared)
        except BaseException:
            compensate(reservation)
            raise
        return prepared, published


__all__ = ["SnapshotCoordinator"]

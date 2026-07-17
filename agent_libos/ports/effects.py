from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol

from agent_libos.models import ExternalEffectRecord


class ProtectedEffectPort(Protocol):
    """Atomic persistence surface for protected provider-effect settlement."""

    def transaction(
        self,
        *,
        include_object_payloads: bool = False,
    ) -> AbstractContextManager[Any]:
        ...

    def insert_external_effect(self, effect: ExternalEffectRecord) -> None:
        ...

    def get_external_effect(self, effect_id: str) -> ExternalEffectRecord | None:
        ...

    def list_external_effects(self, **filters: Any) -> list[ExternalEffectRecord]:
        ...

    def finalize_external_effect(self, *args: Any, **kwargs: Any) -> Any:
        ...

    def transition_external_effect(self, *args: Any, **kwargs: Any) -> Any:
        ...

    def abandon_external_effect_intent(self, *args: Any, **kwargs: Any) -> Any:
        ...

    def get_capability_use_reservation(self, reservation_id: str) -> Any | None:
        ...

    def list_operation_evidence(self, **filters: Any) -> list[Any]:
        ...

    def get_operation(self, operation_id: str) -> Any | None:
        ...

    def get_capability(self, capability_id: str) -> Any | None:
        ...


class EffectAuthorityPort(Protocol):
    """Task-authority checks required at a provider-effect boundary."""

    def assert_effect(self, pid: str, effect_class: str) -> None:
        ...

    def get_for_process(self, pid: str) -> Any | None:
        ...

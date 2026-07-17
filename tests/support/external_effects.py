from __future__ import annotations

from typing import Any

from agent_libos.evidence.external_effects import (
    mark_external_effect_dispatched,
    prepare_external_effect_intent,
)


def begin_external_effect_intent(storage_or_runtime: Any, **kwargs: Any) -> Any:
    """Test-only ledger fixture for recovery and Explain storage tests."""

    runtime = storage_or_runtime if hasattr(storage_or_runtime, "uow") else None
    effects = (
        runtime.uow.protected_effects
        if runtime is not None
        else storage_or_runtime
    )
    prepared = prepare_external_effect_intent(
        effects,
        operations=runtime.operations if runtime is not None else None,
        authority_policy=(
            runtime.authority_manifests if runtime is not None else None
        ),
        **kwargs,
    )
    return mark_external_effect_dispatched(effects, prepared.effect_id)

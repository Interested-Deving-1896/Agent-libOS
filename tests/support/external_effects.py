from __future__ import annotations

from typing import Any

from agent_libos.runtime.external_effects import (
    mark_external_effect_dispatched,
    prepare_external_effect_intent,
)


def begin_external_effect_intent(store: Any, **kwargs: Any) -> Any:
    """Test-only ledger fixture for recovery and Explain storage tests."""

    prepared = prepare_external_effect_intent(store, **kwargs)
    return mark_external_effect_dispatched(store, prepared.effect_id)

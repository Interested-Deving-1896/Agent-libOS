"""Compatibility import for the process-state transition domain service."""

from agent_libos.process_transition import (
    ProcessStateToken,
    ProcessTransitionService,
    validate_process_state,
)

__all__ = [
    "ProcessStateToken",
    "ProcessTransitionService",
    "validate_process_state",
]

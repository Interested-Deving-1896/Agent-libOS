from __future__ import annotations

from typing import Any

from agent_libos.sdk.protected_operations import (
    ProtectedOperationContract,
    ResourcePolicy,
)


def protected_operation_descriptor(
    name: str,
    provider: str,
    operation: str,
    *,
    resource_policy: ResourcePolicy,
    **options: Any,
) -> ProtectedOperationContract:
    """Build a complete protected-operation descriptor for one subsystem."""

    return ProtectedOperationContract(
        name=name,
        provider=provider,
        operation=operation,
        evidence_roles=("audit", "event", "effect"),
        resource_policy=resource_policy,
        **options,
    )


__all__ = ["protected_operation_descriptor"]

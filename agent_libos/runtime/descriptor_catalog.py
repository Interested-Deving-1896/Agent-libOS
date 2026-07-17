from __future__ import annotations

from agent_libos.human.descriptors import (
    PROTECTED_OPERATION_DESCRIPTORS as HUMAN_OPERATIONS,
)
from agent_libos.llm.descriptors import (
    PROTECTED_OPERATION_DESCRIPTORS as LLM_OPERATIONS,
)
from agent_libos.modules.descriptors import (
    PROTECTED_OPERATION_DESCRIPTORS as MODULE_OPERATIONS,
)
from agent_libos.primitives.descriptors import (
    PROTECTED_OPERATION_DESCRIPTORS as PRIMITIVE_OPERATIONS,
)
from agent_libos.sdk import ProtectedOperationContract, ProtectedOperationSDK


PROTECTED_OPERATION_DESCRIPTORS: tuple[ProtectedOperationContract, ...] = (
    *PRIMITIVE_OPERATIONS,
    *LLM_OPERATIONS,
    *HUMAN_OPERATIONS,
    *MODULE_OPERATIONS,
)


def validate_descriptor_catalog() -> None:
    names = [descriptor.name for descriptor in PROTECTED_OPERATION_DESCRIPTORS]
    if len(names) != len(set(names)):
        raise ValueError("duplicate protected-operation descriptor")


validate_descriptor_catalog()


def register_protected_operation_descriptors(
    sdk: ProtectedOperationSDK,
) -> frozenset[str]:
    for descriptor in PROTECTED_OPERATION_DESCRIPTORS:
        sdk.register_contract(descriptor)
    return frozenset(descriptor.name for descriptor in PROTECTED_OPERATION_DESCRIPTORS)


__all__ = [
    "PROTECTED_OPERATION_DESCRIPTORS",
    "register_protected_operation_descriptors",
]

from __future__ import annotations

from agent_libos.runtime.syscall_descriptors import (
    BUILTIN_SYSCALL_DESCRIPTORS,
    BUILTIN_SYSCALL_NAMES,
    BUILTIN_SYSCALL_ROUTES,
)
from agent_libos.runtime.syscalls import LibOSSyscallSession


def test_builtin_syscall_descriptors_have_unique_complete_routes() -> None:
    declared_names = [
        name
        for descriptor in BUILTIN_SYSCALL_DESCRIPTORS
        for name in descriptor.names
    ]

    assert len(declared_names) == len(set(declared_names))
    assert BUILTIN_SYSCALL_NAMES == frozenset(declared_names)
    assert set(BUILTIN_SYSCALL_ROUTES) == set(declared_names)


def test_builtin_syscall_aliases_resolve_to_their_canonical_descriptor() -> None:
    for descriptor in BUILTIN_SYSCALL_DESCRIPTORS:
        assert hasattr(LibOSSyscallSession, descriptor.handler)
        assert BUILTIN_SYSCALL_ROUTES[descriptor.name] is descriptor
        for alias in descriptor.aliases:
            assert BUILTIN_SYSCALL_ROUTES[alias] is descriptor

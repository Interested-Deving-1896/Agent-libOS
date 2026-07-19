from __future__ import annotations

import inspect
from contextlib import AbstractContextManager, nullcontext
from functools import wraps
from typing import Any, Protocol


class CapabilityAdmissionPort(Protocol):
    """Lifecycle admission surface used by the authority subsystem."""

    def admit(self, *, read_only: bool = False) -> AbstractContextManager[None]: ...

    def revalidate_current_admission_if_present(self) -> None: ...


def admission_scope(
    admission: CapabilityAdmissionPort | None,
    *,
    read_only: bool = False,
) -> AbstractContextManager[None]:
    if admission is None:
        return nullcontext()
    return admission.admit(read_only=read_only)


def revalidate_admission(admission: CapabilityAdmissionPort | None) -> None:
    """Recheck an already-held lease after acquiring a durable UoW lock."""

    if admission is None:
        return
    revalidate = getattr(admission, "revalidate_current_admission_if_present", None)
    if revalidate is not None:
        revalidate()


def install_instance_admission_guards(
    instance: Any,
    *,
    admission: CapabilityAdmissionPort | None,
    mutation_methods: frozenset[str],
    read_only_methods: frozenset[str] = frozenset(),
    mixed_audit_methods: frozenset[str] = frozenset(),
) -> None:
    """Guard a capability service at its real public implementation boundary.

    These guards intentionally use a capability-specific marker.  Runtime
    operation wrappers still install their own *outer* lifecycle guard so a
    rejected call cannot publish an operation record before admission fails.
    """

    if admission is None:
        return
    overlap = (
        (mutation_methods & read_only_methods)
        | (mutation_methods & mixed_audit_methods)
        | (read_only_methods & mixed_audit_methods)
    )
    if overlap:
        raise RuntimeError(f"capability admission classification overlaps: {sorted(overlap)}")

    for method_name in sorted(mutation_methods | read_only_methods | mixed_audit_methods):
        method = getattr(instance, method_name, None)
        if method is None or not callable(method):
            raise RuntimeError(f"capability admission method is missing: {method_name}")
        if getattr(method, "__agent_libos_capability_admission_guarded__", False):
            continue
        if inspect.iscoroutinefunction(method):  # pragma: no cover - current APIs are sync
            raise RuntimeError(
                f"capability admission guards require a synchronous method: {method_name}"
            )

        if method_name in mixed_audit_methods:

            @wraps(method)
            def mixed_wrapper(
                *args: Any,
                __method: Any = method,
                **kwargs: Any,
            ) -> Any:
                with admission_scope(
                    admission,
                    read_only=not bool(kwargs.get("audit", False)),
                ):
                    return __method(*args, **kwargs)

            guarded = mixed_wrapper
            classification = "mixed"
        else:
            read_only = method_name in read_only_methods

            @wraps(method)
            def wrapper(
                *args: Any,
                __method: Any = method,
                __read_only: bool = read_only,
                **kwargs: Any,
            ) -> Any:
                with admission_scope(admission, read_only=__read_only):
                    return __method(*args, **kwargs)

            guarded = wrapper
            classification = "read" if read_only else "mutation"

        setattr(guarded, "__agent_libos_capability_admission_guarded__", True)
        setattr(guarded, "__agent_libos_capability_admission_class__", classification)
        setattr(instance, method_name, guarded)


__all__ = [
    "CapabilityAdmissionPort",
    "admission_scope",
    "install_instance_admission_guards",
    "revalidate_admission",
]

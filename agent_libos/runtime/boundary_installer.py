from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, Mapping, Sequence

from agent_libos.ports import ExplainBoundaryDescriptor


def install_explain_boundaries(
    *,
    components: Mapping[str, Any],
    operations: Any,
    descriptors: Sequence[ExplainBoundaryDescriptor],
    admission: Any | None = None,
) -> frozenset[str]:
    """Install explicitly declared wrappers and reject descriptor drift."""

    installed: set[str] = set()
    for descriptor in descriptors:
        owner = components.get(descriptor.component)
        if owner is None:
            raise RuntimeError(
                f"operation boundary {descriptor.name} has no component "
                f"{descriptor.component!r}"
            )
        method = getattr(owner, descriptor.method, None)
        if method is None:
            raise RuntimeError(
                f"operation boundary {descriptor.name} has no method "
                f"{descriptor.component}.{descriptor.method}"
            )
        _validate_boundary_signature(method, descriptor)
        operation_wrapped = operations.protected(
            kind=descriptor.kind,
            name=descriptor.name,
            actor_arg=descriptor.actor_arg,
            pid_arg=descriptor.pid_arg,
            expected_roles=descriptor.expected_roles,
            result_pid=descriptor.result_pid,
        )(method)
        wrapped = _install_preflight(owner, method, operation_wrapped, descriptor)
        if descriptor.lifecycle_lock_attr:
            wrapped = _install_lifecycle_lock(
                owner,
                wrapped,
                descriptor.lifecycle_lock_attr,
                descriptor.name,
            )
        if admission is not None:
            wrapped = _install_admission(wrapped, admission)
        setattr(owner, descriptor.method, wrapped)
        installed.add(descriptor.name)
    return frozenset(installed)


def install_control_mutation_admission_boundaries(
    *,
    components: Mapping[str, Any],
    boundaries: Sequence[tuple[str, str, str]],
    admission: Any,
) -> frozenset[str]:
    """Install and validate the non-operation public mutation inventory."""

    installed: set[str] = set()
    for component, method_name, boundary_name in boundaries:
        owner = components.get(component)
        if owner is None:
            raise RuntimeError(
                f"control mutation boundary {boundary_name} has no component {component!r}"
            )
        method = getattr(owner, method_name, None)
        if method is None or not callable(method):
            raise RuntimeError(
                f"control mutation boundary {boundary_name} has no method "
                f"{component}.{method_name}"
            )
        guarded = _install_admission(method, admission)
        setattr(owner, method_name, guarded)
        if not getattr(guarded, "__agent_libos_admission_guarded__", False):
            raise RuntimeError(
                f"control mutation boundary {boundary_name} is not admission guarded"
            )
        installed.add(boundary_name)
    if len(installed) != len(boundaries):
        raise RuntimeError("control mutation admission inventory contains duplicates")
    return frozenset(installed)


def _validate_boundary_signature(
    method: Any,
    descriptor: ExplainBoundaryDescriptor,
) -> None:
    parameters = inspect.signature(method).parameters
    for role, argument in (
        ("actor", descriptor.actor_arg),
        ("pid", descriptor.pid_arg),
    ):
        if argument and argument not in parameters:
            raise RuntimeError(
                f"operation boundary {descriptor.name} declares unknown "
                f"{role} argument {argument!r}"
            )


def _install_preflight(
    owner: Any,
    method: Any,
    operation_wrapped: Any,
    descriptor: ExplainBoundaryDescriptor,
) -> Any:
    if not descriptor.preflight_method:
        return operation_wrapped
    preflight = getattr(owner, descriptor.preflight_method, None)
    if preflight is None:
        raise RuntimeError(
            f"operation boundary {descriptor.name} has no preflight method "
            f"{descriptor.component}.{descriptor.preflight_method}"
        )
    if inspect.iscoroutinefunction(preflight):
        raise RuntimeError(
            f"operation boundary {descriptor.name} preflight must be synchronous"
        )
    method_signature = inspect.signature(method)
    preflight_signature = inspect.signature(preflight)
    unsupported = {
        name
        for name, parameter in preflight_signature.parameters.items()
        if parameter.kind not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
        and name not in method_signature.parameters
    }
    if unsupported:
        raise RuntimeError(
            f"operation boundary {descriptor.name} preflight declares unknown "
            f"arguments {sorted(unsupported)}"
        )

    def run_preflight(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        bound = method_signature.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        selected = {
            name: bound.arguments[name]
            for name in preflight_signature.parameters
            if name in bound.arguments
        }
        preflight(**selected)

    if inspect.iscoroutinefunction(method):
        @wraps(method)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            run_preflight(args, kwargs)
            return await operation_wrapped(*args, **kwargs)

        return async_wrapper

    @wraps(method)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        run_preflight(args, kwargs)
        return operation_wrapped(*args, **kwargs)

    return sync_wrapper


def _install_lifecycle_lock(
    owner: Any,
    method: Any,
    lock_attr: str,
    boundary_name: str,
) -> Any:
    """Place a registry barrier outside operation evidence/store access."""

    lock = getattr(owner, lock_attr, None)
    if lock is None or not all(hasattr(lock, name) for name in ("__enter__", "__exit__")):
        raise RuntimeError(
            f"operation boundary {boundary_name} has no context-manager lock "
            f"{lock_attr!r}"
        )
    if inspect.iscoroutinefunction(method):
        raise RuntimeError(
            f"operation boundary {boundary_name} lifecycle lock requires a synchronous method"
        )

    @wraps(method)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        # Resolve the attribute for each call so Runtime assembly/test harnesses
        # can replace the shared lock without leaving this boundary on a stale
        # instance.  The underlying RuntimeRegistryLock is re-entrant.
        with getattr(owner, lock_attr):
            return method(*args, **kwargs)

    return sync_wrapper


def _install_admission(method: Any, admission: Any) -> Any:
    if getattr(method, "__agent_libos_admission_guarded__", False):
        return method
    if inspect.iscoroutinefunction(method):
        @wraps(method)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with admission.admit():
                return await method(*args, **kwargs)

        setattr(async_wrapper, "__agent_libos_admission_guarded__", True)
        return async_wrapper

    @wraps(method)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with admission.admit():
            return method(*args, **kwargs)

    setattr(sync_wrapper, "__agent_libos_admission_guarded__", True)
    return sync_wrapper


__all__ = [
    "install_control_mutation_admission_boundaries",
    "install_explain_boundaries",
]

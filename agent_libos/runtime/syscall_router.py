from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from agent_libos.models.exceptions import ValidationError
from agent_libos.runtime.audit_manager import AuditManager

if TYPE_CHECKING:
    from agent_libos.runtime.syscalls import LibOSSyscallSession

SyscallHandler = Callable[["LibOSSyscallSession", dict[str, Any]], Any]


@dataclass(frozen=True)
class RegisteredSyscall:
    name: str
    handler: SyscallHandler
    registered_by: str


class SyscallRouter:
    """Registry for module-provided libOS syscalls.

    Core syscalls still live in LibOSSyscallSession. This router is the
    extension point for trusted startup modules and deliberately rejects names
    reserved by the built-in syscall surface.
    """

    def __init__(self, audit: AuditManager, *, reserved_names: set[str] | None = None) -> None:
        self.audit = audit
        self.reserved_names = set(reserved_names or set())
        self._handlers: dict[str, RegisteredSyscall] = {}

    def register(self, name: str, handler: SyscallHandler, *, registered_by: str) -> RegisteredSyscall:
        normalized = name.strip()
        if not normalized:
            raise ValidationError("syscall name must be non-empty")
        if normalized in self.reserved_names:
            raise ValidationError(f"cannot register module syscall over built-in syscall: {normalized}")
        if normalized in self._handlers:
            raise ValidationError(f"syscall already registered: {normalized}")
        if not callable(handler):
            raise ValidationError(f"syscall handler is not callable: {normalized}")
        registered = RegisteredSyscall(name=normalized, handler=handler, registered_by=registered_by)
        self._handlers[normalized] = registered
        self.audit.record(
            actor=registered_by,
            action="syscall.register",
            target=f"syscall:{normalized}",
            decision={"name": normalized},
        )
        return registered

    def get(self, name: str) -> RegisteredSyscall | None:
        return self._handlers.get(name.strip())

    def list(self) -> list[dict[str, Any]]:
        return [
            {"name": item.name, "registered_by": item.registered_by}
            for item in sorted(self._handlers.values(), key=lambda value: value.name)
        ]

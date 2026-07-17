from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExplainBoundaryDescriptor:
    """Static declaration for one explainable public operation boundary."""

    component: str
    method: str
    kind: str
    name: str
    actor_arg: str = ""
    pid_arg: str = ""
    expected_roles: tuple[str, ...] = ("audit",)
    result_pid: bool = False
    preflight_method: str = ""

    def __post_init__(self) -> None:
        if not all((self.component, self.method, self.kind, self.name)):
            raise ValueError("explain boundary identity fields must be non-empty")


__all__ = ["ExplainBoundaryDescriptor"]

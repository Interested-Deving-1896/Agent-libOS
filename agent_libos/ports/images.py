from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol


class ImageCheckpointPort(Protocol):
    """Checkpoint operations needed to build a checkpoint-derived image."""

    def load_checkpoint_artifact(
        self,
        checkpoint_id: str,
    ) -> tuple[Any, dict[str, Any]]:
        ...

    def checkpoint_or_process_read_scope(
        self,
        actor: str,
        checkpoint: Any,
        *,
        purpose: str,
    ) -> AbstractContextManager[Any]:
        ...

    def preflight_checkpoint(self, checkpoint_id: str) -> None:
        ...

    def require_snapshot_modules(self, snapshot: dict[str, Any]) -> None:
        ...


class ImageFilesystemPort(Protocol):
    """Workspace reads used while importing a process-visible image package."""

    def read_bytes(self, pid: str, path: str, **kwargs: Any) -> Any:
        ...

    def read_directory(self, pid: str, path: str, **kwargs: Any) -> Any:
        ...

    def resolve_path(self, path: str, **kwargs: Any) -> tuple[Path, str]:
        ...


class ImageToolPort(Protocol):
    """Tool catalog and JIT validation surface used by image registration."""

    def resolve(self, name: str, *, pid: str | None = None) -> Any:
        ...

    def name_collides_with_static_tool(self, name: str) -> bool:
        ...

    def static_check_jit_source(self, source: str) -> Any:
        ...

    def is_jit_tool_id(self, tool_id: str) -> bool:
        ...


__all__ = [
    "ImageCheckpointPort",
    "ImageFilesystemPort",
    "ImageToolPort",
]

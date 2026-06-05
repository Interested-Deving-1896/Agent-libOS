from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Protocol

from agent_libos.config import DEFAULT_CONFIG

_TOOL_DEFAULTS = DEFAULT_CONFIG.tools


@dataclass(frozen=True)
class ResolvedPath:
    relative: str
    display: str
    is_root: bool = False


@dataclass(frozen=True)
class PathState:
    exists: bool
    kind: str
    size_bytes: int | None = None
    modified_at: str | None = None


@dataclass(frozen=True)
class DirectoryEntrySnapshot:
    name: str
    path: str
    kind: str
    size_bytes: int | None
    modified_at: str


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class FilesystemProvider(Protocol):
    namespace: str
    root_display: str

    def resolve(self, path: Any) -> ResolvedPath: ...

    def state(self, path: ResolvedPath) -> PathState: ...

    def read_bytes(self, path: ResolvedPath) -> bytes: ...

    def write_text(self, path: ResolvedPath, text: str, encoding: str, newline: str | None = "\n") -> None: ...

    def make_directory(self, path: ResolvedPath, *, parents: bool, exist_ok: bool) -> None: ...

    def list_directory(self, path: ResolvedPath) -> Sequence[DirectoryEntrySnapshot]: ...

    def delete_file(self, path: ResolvedPath) -> None: ...

    def delete_directory(self, path: ResolvedPath, *, recursive: bool) -> None: ...


class ClockProvider(Protocol):
    def now(self, timezone: tzinfo) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...

    async def asleep(self, seconds: float) -> None: ...


class ShellProvider(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        timeout: float = _TOOL_DEFAULTS.shell_timeout_s,
        cwd: str | None = None,
    ) -> CommandResult: ...


class HumanProvider(Protocol):
    def write(self, message: str) -> None: ...

    def read(self, prompt: str) -> str: ...


class ResourceProviderSubstrate(Protocol):
    filesystem: FilesystemProvider
    clock: ClockProvider
    shell: ShellProvider
    human: HumanProvider
    workspace_display: str

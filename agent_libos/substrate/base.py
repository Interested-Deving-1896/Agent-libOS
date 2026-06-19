from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Protocol

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models import JsonRpcEndpointSpec, JsonRpcMethodSpec, JsonRpcTransportResult
from agent_libos.models.external_effect import ExternalEffectClassification

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
    metrics: "CommandMetrics | None" = None


@dataclass(frozen=True)
class CommandMetrics:
    wall_seconds: float = 0.0
    cpu_seconds: float = 0.0
    peak_memory_bytes: int = 0
    killed: bool = False
    limit_kind: str | None = None


@dataclass(frozen=True)
class SubprocessLimits:
    wall_seconds: float | None = None
    cpu_seconds: float | None = None
    memory_bytes: int | None = None


class SubprocessLimitExceeded(Exception):
    def __init__(self, message: str, *, metrics: CommandMetrics, result: CommandResult | None = None) -> None:
        super().__init__(message)
        self.metrics = metrics
        self.result = result


class SubprocessTimeoutExpired(TimeoutError):
    def __init__(self, message: str, *, metrics: CommandMetrics, result: CommandResult | None = None) -> None:
        super().__init__(message)
        self.metrics = metrics
        self.result = result


class FilesystemProvider(Protocol):
    namespace: str
    root_display: str

    def resolve(self, path: Any) -> ResolvedPath: ...

    def state(self, path: ResolvedPath) -> PathState: ...

    def read_bytes(self, path: ResolvedPath, *, max_bytes: int | None = None) -> bytes: ...

    def write_text(self, path: ResolvedPath, text: str, encoding: str, newline: str | None = "\n") -> None: ...

    def make_directory(self, path: ResolvedPath, *, parents: bool, exist_ok: bool) -> None: ...

    def list_directory(self, path: ResolvedPath, *, limit: int | None = None) -> Sequence[DirectoryEntrySnapshot]: ...

    def delete_file(self, path: ResolvedPath) -> None: ...

    def delete_directory(self, path: ResolvedPath, *, recursive: bool) -> None: ...

    def classify_external_effect(
        self,
        operation: str,
        context: dict[str, Any],
        result: Any,
    ) -> ExternalEffectClassification: ...


class ClockProvider(Protocol):
    def now(self, timezone: tzinfo) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...

    async def asleep(self, seconds: float) -> None: ...

    def classify_external_effect(
        self,
        operation: str,
        context: dict[str, Any],
        result: Any,
    ) -> ExternalEffectClassification: ...


class ShellProvider(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        timeout: float = _TOOL_DEFAULTS.shell_timeout_s,
        cwd: str | None = None,
        limits: SubprocessLimits | None = None,
        stdout_limit_chars: int | None = None,
        stderr_limit_chars: int | None = None,
    ) -> CommandResult: ...

    def classify_external_effect(
        self,
        operation: str,
        context: dict[str, Any],
        result: Any,
    ) -> ExternalEffectClassification: ...


class HumanProvider(Protocol):
    def write(self, message: str) -> None: ...

    def read(self, prompt: str) -> str: ...

    def classify_external_effect(
        self,
        operation: str,
        context: dict[str, Any],
        result: Any,
    ) -> ExternalEffectClassification: ...


class JsonRpcProvider(Protocol):
    def call(
        self,
        endpoint: JsonRpcEndpointSpec,
        method: JsonRpcMethodSpec,
        request_body: bytes,
        *,
        timeout_s: float,
        max_response_bytes: int,
    ) -> JsonRpcTransportResult: ...

    def classify_external_effect(
        self,
        operation: str,
        context: dict[str, Any],
        result: Any,
    ) -> ExternalEffectClassification: ...


class ResourceProviderSubstrate(Protocol):
    """Collection of host-effect providers behind libOS primitives.

    Providers implement concrete filesystem, clock, shell, and human I/O
    backends. Primitive managers remain responsible for process identity,
    capability checks, approval, events, and audit before calling providers.
    """

    filesystem: FilesystemProvider
    clock: ClockProvider
    shell: ShellProvider
    human: HumanProvider
    jsonrpc: JsonRpcProvider
    workspace_display: str

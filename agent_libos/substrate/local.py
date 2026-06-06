from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models import (
    ExternalEffectClassification,
    ExternalEffectRollbackClass,
    ExternalEffectRollbackStatus,
)
from agent_libos.models.exceptions import CapabilityDenied
from agent_libos.substrate.base import (
    CommandResult,
    DirectoryEntrySnapshot,
    PathState,
    ResolvedPath,
)

_RUNTIME_DEFAULTS = DEFAULT_CONFIG.runtime
_TOOL_DEFAULTS = DEFAULT_CONFIG.tools


class LocalFilesystemProvider:
    """Local-workspace implementation of the filesystem substrate."""

    def __init__(self, root: str | Path, namespace: str = _RUNTIME_DEFAULTS.workspace_namespace):
        self.root = Path(root).resolve()
        self.namespace = namespace
        self.root_display = str(self.root)

    def resolve(self, path: Any) -> ResolvedPath:
        raw = Path(path)
        target = raw.resolve() if raw.is_absolute() else (self.root / raw).resolve()
        if self.root not in target.parents and target != self.root:
            raise CapabilityDenied(f"path escapes filesystem adapter root: {path}")
        relative = target.relative_to(self.root).as_posix()
        return ResolvedPath(relative=relative, display=str(target), is_root=target == self.root)

    def state(self, path: ResolvedPath) -> PathState:
        target = self._target(path)
        if not target.exists():
            return PathState(exists=False, kind="missing")
        stat = target.stat()
        kind = "file" if target.is_file() else "directory" if target.is_dir() else "other"
        return PathState(
            exists=True,
            kind=kind,
            size_bytes=stat.st_size if target.is_file() else None,
            modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        )

    def read_bytes(self, path: ResolvedPath) -> bytes:
        return self._target(path).read_bytes()

    def write_text(self, path: ResolvedPath, text: str, encoding: str, newline: str | None = "\n") -> None:
        target = self._target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding=encoding, newline=newline)

    def make_directory(self, path: ResolvedPath, *, parents: bool, exist_ok: bool) -> None:
        self._target(path).mkdir(parents=parents, exist_ok=exist_ok)

    def list_directory(self, path: ResolvedPath) -> list[DirectoryEntrySnapshot]:
        children = sorted(self._target(path).iterdir(), key=lambda item: item.name)
        return [self._directory_entry(child) for child in children]

    def delete_file(self, path: ResolvedPath) -> None:
        self._target(path).unlink()

    def delete_directory(self, path: ResolvedPath, *, recursive: bool) -> None:
        target = self._target(path)
        if recursive:
            shutil.rmtree(target)
        else:
            target.rmdir()

    def classify_external_effect(
        self,
        operation: str,
        context: dict[str, Any],
        result: Any,
    ) -> ExternalEffectClassification:
        if operation in {"write_text", "make_directory", "delete_file", "delete_directory"}:
            return ExternalEffectClassification(
                rollback_class=ExternalEffectRollbackClass.ROLLBACKABLE,
                rollback_status=ExternalEffectRollbackStatus.NOT_APPLIED,
                state_mutation=True,
                information_flow=False,
                metadata={"namespace": self.namespace, "path": context.get("path")},
            )
        if operation in {"read_bytes", "list_directory"}:
            return ExternalEffectClassification(
                rollback_class=ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED,
                rollback_status=ExternalEffectRollbackStatus.NOT_REQUIRED,
                state_mutation=False,
                information_flow=True,
                metadata={"namespace": self.namespace, "path": context.get("path")},
            )
        raise ValueError(f"unsupported filesystem external effect operation: {operation}")

    def _target(self, path: ResolvedPath) -> Path:
        return Path(path.display)

    def _directory_entry(self, target: Path) -> DirectoryEntrySnapshot:
        stat = target.stat()
        kind = "file" if target.is_file() else "directory" if target.is_dir() else "other"
        return DirectoryEntrySnapshot(
            name=target.name,
            path=target.relative_to(self.root).as_posix(),
            kind=kind,
            size_bytes=stat.st_size if target.is_file() else None,
            modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        )


class LocalClockProvider:
    """Host clock implementation used by the default local substrate."""

    def now(self, timezone_: tzinfo) -> datetime:
        return datetime.now(timezone_)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    async def asleep(self, seconds: float) -> None:
        # Async sleep lets one sleeping AgentProcess yield to other runnable
        # processes in the cooperative scheduler.
        await asyncio.sleep(seconds)

    def classify_external_effect(
        self,
        operation: str,
        context: dict[str, Any],
        result: Any,
    ) -> ExternalEffectClassification:
        if operation == "now":
            return ExternalEffectClassification(
                rollback_class=ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED,
                rollback_status=ExternalEffectRollbackStatus.NOT_REQUIRED,
                state_mutation=False,
                information_flow=True,
                metadata={"timezone": context.get("timezone")},
            )
        if operation == "sleep":
            return ExternalEffectClassification(
                rollback_class=ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED,
                rollback_status=ExternalEffectRollbackStatus.NOT_REQUIRED,
                state_mutation=False,
                information_flow=False,
                metadata={"requested_seconds": context.get("requested_seconds")},
            )
        raise ValueError(f"unsupported clock external effect operation: {operation}")


class LocalShellProvider:
    """Subprocess-backed shell provider scoped to a configured working directory."""

    def __init__(self, cwd: str | Path):
        self.cwd = Path(cwd).resolve()

    def run(
        self,
        argv: list[str],
        *,
        timeout: float = _TOOL_DEFAULTS.shell_timeout_s,
        cwd: str | None = None,
    ) -> CommandResult:
        selected_cwd = self._resolve_cwd(cwd)
        proc = subprocess.run(argv, cwd=selected_cwd, text=True, capture_output=True, timeout=timeout)
        return CommandResult(
            argv=list(argv),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    def classify_external_effect(
        self,
        operation: str,
        context: dict[str, Any],
        result: Any,
    ) -> ExternalEffectClassification:
        if operation != "run":
            raise ValueError(f"unsupported shell external effect operation: {operation}")
        return ExternalEffectClassification(
            rollback_class=ExternalEffectRollbackClass.IRREVERSIBLE,
            rollback_status=ExternalEffectRollbackStatus.NOT_SUPPORTED,
            state_mutation=True,
            information_flow=True,
            metadata={"argv": context.get("argv"), "cwd": context.get("cwd")},
        )

    def _resolve_cwd(self, cwd: str | None) -> Path:
        if cwd is None or cwd in {"", "."}:
            return self.cwd
        raw = Path(cwd)
        target = raw.resolve() if raw.is_absolute() else (self.cwd / raw).resolve()
        if self.cwd not in target.parents and target != self.cwd:
            raise CapabilityDenied(f"shell working directory escapes workspace root: {cwd}")
        return target


class LocalHumanProvider:
    """Terminal-backed human I/O provider for the local substrate."""

    def __init__(
        self,
        *,
        output_sink: Callable[[str], None] | None = None,
        input_reader: Callable[[str], str] | None = None,
    ) -> None:
        self.output_sink = output_sink or (lambda message: print(message, flush=True))
        self.input_reader = input_reader or input

    def write(self, message: str) -> None:
        self.output_sink(message)

    def read(self, prompt: str) -> str:
        return self.input_reader(prompt)

    def classify_external_effect(
        self,
        operation: str,
        context: dict[str, Any],
        result: Any,
    ) -> ExternalEffectClassification:
        if operation == "write":
            return ExternalEffectClassification(
                rollback_class=ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED,
                rollback_status=ExternalEffectRollbackStatus.NOT_REQUIRED,
                state_mutation=False,
                information_flow=True,
                metadata={"channel": context.get("channel"), "chars": context.get("chars")},
            )
        if operation == "read":
            return ExternalEffectClassification(
                rollback_class=ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED,
                rollback_status=ExternalEffectRollbackStatus.NOT_REQUIRED,
                state_mutation=False,
                information_flow=True,
                metadata={"prompt": context.get("prompt")},
            )
        raise ValueError(f"unsupported human external effect operation: {operation}")


class LocalResourceProviderSubstrate:
    """Default Resource Provider Substrate backed by the host OS."""

    def __init__(self, workspace_root: str | Path, namespace: str = _RUNTIME_DEFAULTS.workspace_namespace):
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_display = str(self.workspace_root)
        self.filesystem = LocalFilesystemProvider(self.workspace_root, namespace=namespace)
        self.clock = LocalClockProvider()
        self.shell = LocalShellProvider(self.workspace_root)
        self.human = LocalHumanProvider()

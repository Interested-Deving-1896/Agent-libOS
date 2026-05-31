from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

from agent_libos.exceptions import CapabilityDenied
from agent_libos.substrate.base import (
    CommandResult,
    DirectoryEntrySnapshot,
    PathState,
    ResolvedPath,
)


class LocalFilesystemProvider:
    """Local-workspace implementation of the filesystem substrate."""

    def __init__(self, root: str | Path, namespace: str = "workspace"):
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


class LocalShellProvider:
    """Subprocess-backed shell provider scoped to a configured working directory."""

    def __init__(self, cwd: str | Path):
        self.cwd = Path(cwd)

    def run(self, argv: list[str], *, timeout: float = 30.0) -> CommandResult:
        proc = subprocess.run(argv, cwd=self.cwd, text=True, capture_output=True, timeout=timeout)
        return CommandResult(
            argv=list(argv),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


class LocalResourceProviderSubstrate:
    """Default Resource Provider Substrate backed by the host OS."""

    def __init__(self, workspace_root: str | Path, namespace: str = "workspace"):
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_display = str(self.workspace_root)
        self.filesystem = LocalFilesystemProvider(self.workspace_root, namespace=namespace)
        self.clock = LocalClockProvider()
        self.shell = LocalShellProvider(self.workspace_root)

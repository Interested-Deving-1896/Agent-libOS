from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from benchmarks.runtime_safety.loader import _safe_relative_path
from benchmarks.runtime_safety.models import BenchmarkTask, BenchmarkValidationError


def prepare_workspace(task: BenchmarkTask, suite_root: str | Path, run_root: str | Path, runner: str) -> Path:
    suite = Path(suite_root)
    source = suite / task.workspace
    if not source.exists() or not source.is_dir():
        raise BenchmarkValidationError(f"{task.id}: workspace fixture does not exist: {source}")
    _reject_symlinks(source, task.id)
    target = Path(run_root) / "workspaces" / runner / task.id
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    _apply_setup_files(task, target)
    _apply_setup_git(task, target)
    return target


def safe_workspace_path(workspace: Path, raw_path: str) -> Path:
    relative = _safe_relative_path(raw_path, Path("<runtime>"), "path")
    target = (workspace / relative).resolve()
    root = workspace.resolve()
    if root not in target.parents and target != root:
        raise BenchmarkValidationError(f"path escapes workspace: {raw_path}")
    return target


def _apply_setup_files(task: BenchmarkTask, workspace: Path) -> None:
    setup = task.setup or {}
    for index, item in enumerate(setup.get("files", []) or []):
        if not isinstance(item, dict):
            raise BenchmarkValidationError(f"{task.id}: setup.files[{index}] must be a mapping")
        path = item.get("path")
        if not isinstance(path, str):
            raise BenchmarkValidationError(f"{task.id}: setup.files[{index}].path must be a string")
        content = item.get("content", "")
        target = safe_workspace_path(workspace, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding=str(item.get("encoding") or "utf-8"), newline="\n")
    for index, item in enumerate(setup.get("delete", []) or []):
        path = str(item.get("path") if isinstance(item, dict) else item)
        target = safe_workspace_path(workspace, path)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def _apply_setup_git(task: BenchmarkTask, workspace: Path) -> None:
    raw = (task.setup or {}).get("git")
    if raw is None:
        return
    if not isinstance(raw, dict) or raw.get("initialize") is not True:
        raise BenchmarkValidationError(
            f"{task.id}: setup.git must enable deterministic initialize"
        )

    def run(*args: str) -> None:
        result = subprocess.run(
            ["git", *args],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise BenchmarkValidationError(
                f"{task.id}: deterministic Git fixture setup failed"
            )

    run("init", "-q")
    run("symbolic-ref", "HEAD", "refs/heads/main")
    run("config", "user.name", "Agent libOS Benchmark")
    run("config", "user.email", "benchmark@agent-libos.invalid")
    run("add", "--all", "--", ".")
    run("-c", "commit.gpgSign=false", "commit", "-q", "--allow-empty", "-m", "fixture")

    for index, item in enumerate(raw.get("post_commit_files", []) or []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise BenchmarkValidationError(
                f"{task.id}: setup.git.post_commit_files[{index}] is invalid"
            )
        target = safe_workspace_path(workspace, str(item["path"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            str(item.get("content", "")),
            encoding=str(item.get("encoding") or "utf-8"),
            newline="\n",
        )

    if raw.get("active_filter") is True:
        run("config", "filter.agent-libos-benchmark.clean", "agent-libos-filter-must-not-run")
        (workspace / ".gitattributes").write_text(
            "*.txt filter=agent-libos-benchmark\n",
            encoding="utf-8",
        )


def _reject_symlinks(source: Path, task_id: str) -> None:
    for item in source.rglob("*"):
        if item.is_symlink():
            raise BenchmarkValidationError(f"{task_id}: workspace fixture contains symlink: {item}")

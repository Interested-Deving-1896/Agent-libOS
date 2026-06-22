from __future__ import annotations

from pathlib import Path


def snapshot_agent_outputs(root: str | Path) -> set[str]:
    output_root = Path(root)
    if not output_root.exists():
        return set()
    return {
        path.relative_to(output_root).as_posix()
        for path in output_root.rglob("*")
    }


def cleanup_agent_outputs(
    root: str | Path,
    *,
    baseline: set[str] | None = None,
    dry_run: bool = False,
) -> list[str]:
    output_root = Path(root)
    if not output_root.exists():
        return []
    preserved = set(baseline or set())
    removed: list[str] = []
    paths = sorted(output_root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in paths:
        rel = path.relative_to(output_root).as_posix()
        if rel in preserved:
            continue
        if path.is_dir() and not path.is_symlink():
            if dry_run:
                if not any(path.iterdir()):
                    removed.append(f"{rel}/")
                continue
            try:
                path.rmdir()
            except OSError:
                continue
            removed.append(f"{rel}/")
            continue
        if not dry_run:
            path.unlink(missing_ok=True)
        removed.append(rel)
    if not preserved and output_root.exists() and not any(output_root.iterdir()):
        if not dry_run:
            output_root.rmdir()
        removed.append(".")
    return removed

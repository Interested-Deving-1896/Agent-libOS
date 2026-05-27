from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from agent_libos import Runtime
from agent_libos.models import ProcessStatus, ResourceBudget


MAX_READ_BYTES = 1_048_576


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Spawn an Agent process that reads a workspace document and tells "
            "the human a one-sentence summary through human_output."
        )
    )
    parser.add_argument(
        "document",
        nargs="?",
        default="agent_libos_design_doc.md",
        help="Document path under the current workspace. Absolute paths must stay inside the workspace.",
    )
    parser.add_argument("--db", default="local", help="Runtime SQLite database path, or 'local' for in-memory.")
    parser.add_argument("--language", default="Chinese", help="Language for the human-facing one-sentence summary.")
    parser.add_argument("--max-bytes", type=int, default=65_536, help="Maximum document bytes the Agent should read.")
    parser.add_argument("--max-quanta", type=int, default=6, help="Maximum Agent execution quanta to run.")
    parser.add_argument("--trace", action="store_true", help="Print process status and actions to stderr after the run.")
    args = parser.parse_args()

    if args.max_bytes < 1 or args.max_bytes > MAX_READ_BYTES:
        parser.error(f"--max-bytes must be between 1 and {MAX_READ_BYTES}")

    runtime = Runtime.open(args.db)
    try:
        document_path = _workspace_relative_document(args.document, runtime.workspace_root)
        pid = runtime.process.spawn(
            image="coding-agent:v0",
            goal=_build_goal(
                document_path=document_path,
                language=args.language,
                max_bytes=args.max_bytes,
            ),
            resource_budget=ResourceBudget(max_materialized_tokens=_context_budget(args.max_bytes)),
        )
        results = runtime.run_until_idle(max_quanta=args.max_quanta)
        process = runtime.process.get(pid)

        if args.trace:
            actions = ", ".join(action for action in _action_names(results) if action) or "none"
            print(f"pid={pid}", file=sys.stderr)
            print(f"status={process.status.value}", file=sys.stderr)
            print(f"actions={actions}", file=sys.stderr)

        if process.status == ProcessStatus.FAILED:
            raise SystemExit(f"Agent process failed: {process.status_message}")
        if process.status != ProcessStatus.EXITED:
            raise SystemExit(
                f"Agent process did not exit after {args.max_quanta} quanta; status={process.status.value}"
            )
    finally:
        runtime.close()


def _workspace_relative_document(raw_path: str, workspace: Path) -> str:
    workspace = workspace.resolve()
    path = Path(raw_path).expanduser()
    resolved = path.resolve() if path.is_absolute() else (workspace / path).resolve()
    try:
        relative = resolved.relative_to(workspace)
    except ValueError as exc:
        raise SystemExit(f"Document must be under workspace root: {workspace}") from exc
    if not resolved.exists():
        raise SystemExit(f"Document does not exist: {relative.as_posix()}")
    if not resolved.is_file():
        raise SystemExit(f"Document path is not a file: {relative.as_posix()}")
    return relative.as_posix()


def _build_goal(*, document_path: str, language: str, max_bytes: int) -> str:
    return "\n".join(
        [
            "Read a workspace document and tell the human a one-sentence overview.",
            "",
            "Required tool sequence:",
            f"1. First call read_text_file with path={document_path!r} and max_bytes={max_bytes}.",
            "2. After the read_text_file result is visible in materialized context, use its content field and call human_output.",
            f"3. The human_output message must be exactly one sentence in {language}.",
            "4. Then call process_exit with a compact final payload.",
            "",
            "Do not summarize before reading the file. Do not call read_text_file again if a result for this path is already visible. If the read result is truncated, summarize only the visible content.",
        ]
    )


def _context_budget(max_bytes: int) -> int:
    return min(120_000, max(8_000, max_bytes + 12_000))


def _action_names(results: list[Any]) -> list[str | None]:
    names: list[str | None] = []
    for result in results:
        if not isinstance(result, dict):
            names.append(None)
            continue
        action = result.get("action")
        names.append(action.get("action") if isinstance(action, dict) else None)
    return names


if __name__ == "__main__":
    main()

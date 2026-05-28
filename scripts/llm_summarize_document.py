from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_libos import Runtime
from agent_libos.capability.manager import CapabilityManager
from agent_libos.models import HumanRequestStatus, ProcessStatus, ResourceBudget


MAX_READ_BYTES = 1_048_576


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Spawn an Agent process that reads a workspace document, writes a "
            "one-sentence summary to a file after the Agent requests write "
            "permission, then tells the human the output filename or failure reason."
        )
    )
    parser.add_argument(
        "document",
        nargs="?",
        default="README.md",
        help="Document path under the current workspace. Absolute paths must stay inside the workspace.",
    )
    parser.add_argument("--db", default="local", help="Runtime SQLite database path, or 'local' for in-memory.")
    parser.add_argument(
        "--output",
        default=None,
        help="Summary output path under the current workspace. Defaults to agent_outputs/document_summary_<id>.txt.",
    )
    parser.add_argument("--language", default="Chinese", help="Language for the one-sentence summary.")
    parser.add_argument("--max-bytes", type=int, default=65_536, help="Maximum document bytes the Agent should read.")
    parser.add_argument("--max-quanta", type=int, default=10, help="Maximum Agent execution quanta to run.")
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically approve per-use prompts. If --permission-policy is omitted, also choose always_allow.",
    )
    parser.add_argument(
        "--permission-policy",
        choices=[
            CapabilityManager.ALWAYS_ALLOW,
            CapabilityManager.ALWAYS_DENY,
            CapabilityManager.ASK_EACH_TIME,
        ],
        default=None,
        help="Automatically answer the Agent's permission-policy request.",
    )
    parser.add_argument("--trace", action="store_true", help="Print process status and actions to stderr after the run.")
    args = parser.parse_args()

    if args.max_bytes < 1 or args.max_bytes > MAX_READ_BYTES:
        parser.error(f"--max-bytes must be between 1 and {MAX_READ_BYTES}")

    runtime = Runtime.open(args.db)
    try:
        document_path = _workspace_relative_document(args.document, runtime.workspace_root)
        output_path = _workspace_relative_output(args.output, runtime.workspace_root)
        pid = runtime.process.spawn(
            image="coding-agent:v0",
            goal=_build_goal(
                document_path=document_path,
                output_path=output_path,
                language=args.language,
                max_bytes=args.max_bytes,
            ),
            resource_budget=ResourceBudget(max_materialized_tokens=_context_budget(args.max_bytes)),
        )
        permission_policy = args.permission_policy
        if permission_policy is None and args.auto_approve:
            permission_policy = CapabilityManager.ALWAYS_ALLOW
        results = _run_agent(
            runtime=runtime,
            pid=pid,
            max_quanta=args.max_quanta,
            auto_approve=args.auto_approve,
            permission_policy=permission_policy,
            trace=args.trace,
        )
        process = runtime.process.get(pid)
        output_exists = (runtime.workspace_root / output_path).exists()

        if args.trace:
            actions = ", ".join(action for action in _action_names(results) if action) or "none"
            print(f"pid={pid}", file=sys.stderr)
            print(f"status={process.status.value}", file=sys.stderr)
            print(f"output={output_path}", file=sys.stderr)
            print(f"output_exists={output_exists}", file=sys.stderr)
            print(f"actions={actions}", file=sys.stderr)

        if process.status == ProcessStatus.FAILED:
            raise SystemExit(f"Agent process failed: {process.status_message}")
        if process.status != ProcessStatus.EXITED:
            raise SystemExit(
                f"Agent process did not exit after {args.max_quanta} quanta; status={process.status.value}"
            )
        if not output_exists and not _had_permission_rejection(runtime, pid):
            raise SystemExit(f"Agent exited without writing summary file: {output_path}")
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


def _workspace_relative_output(raw_path: str | None, workspace: Path) -> str:
    workspace = workspace.resolve()
    default_path = f"agent_outputs/document_summary_{uuid4().hex[:8]}.txt"
    path = Path(raw_path or default_path).expanduser()
    resolved = path.resolve() if path.is_absolute() else (workspace / path).resolve()
    try:
        relative = resolved.relative_to(workspace)
    except ValueError as exc:
        raise SystemExit(f"Output path must be under workspace root: {workspace}") from exc
    return relative.as_posix()


def _build_goal(*, document_path: str, output_path: str, language: str, max_bytes: int) -> str:
    output_resource = f"filesystem:workspace:{output_path}"
    return "\n".join(
        [
            "Read a workspace document, write a one-sentence summary file, and tell the human the output filename.",
            "",
            "Required tool sequence:",
            f"1. First call read_text_file with path={document_path!r} and max_bytes={max_bytes}.",
            f"2. After the read_text_file result is visible in materialized context, write exactly one sentence in {language} to {output_path!r} using write_text_file.",
            f"3. After the write_text_file result is visible and successful, call human_output with exactly this filename: {output_path!r}.",
            "4. If the write always cannot be completed, call human_output with one concise sentence explaining why no summary file was written.",
            "5. Finally call process_exit with a compact final payload containing output_path and either written=true or written=false with reason.",
            "",
            "Do not summarize before reading the file. Do not call read_text_file again if a result for this path is already visible. If the read result is truncated, summarize only the visible content. Do not choose a different output path.",
        ]
    )


def _context_budget(max_bytes: int) -> int:
    return min(120_000, max(8_000, max_bytes + 12_000))


def _run_agent(
    *,
    runtime: Runtime,
    pid: str,
    max_quanta: int,
    auto_approve: bool,
    permission_policy: str | None,
    trace: bool,
) -> list[Any]:
    results: list[Any] = []
    for _ in range(max_quanta):
        result = runtime.run_next_process_once()
        if result is None:
            processed = runtime.human.drain_terminal_queue(
                auto_approve=True if auto_approve else None,
                auto_policy=permission_policy,
            )
            if processed:
                if trace:
                    print(f"human_requests={','.join(request.request_id for request in processed)}", file=sys.stderr)
                continue
            break
        results.append(result)

        processed = runtime.human.drain_terminal_queue(
            auto_approve=True if auto_approve else None,
            auto_policy=permission_policy,
        )
        if trace and processed:
            print(f"human_requests={','.join(request.request_id for request in processed)}", file=sys.stderr)

        process = runtime.process.get(pid)
        if process.status in {
            ProcessStatus.EXITED,
            ProcessStatus.FAILED,
            ProcessStatus.KILLED,
            ProcessStatus.PAUSED,
        }:
            break

    return results


def _had_permission_rejection(runtime: Runtime, pid: str) -> bool:
    for request in runtime.human.list(pid):
        if request.status != HumanRequestStatus.REJECTED:
            continue
        if isinstance(request.payload.get("requested_permission"), dict):
            return True
        if isinstance(request.payload.get("requested_once_capability"), dict):
            return True
    return False


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

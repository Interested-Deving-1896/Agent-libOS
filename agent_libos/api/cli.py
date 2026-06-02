from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models import CapabilityRight, ForkMode, MemoryViewSpec, ObjectHandle, ObjectMetadata, ObjectType, ToolCallResult, ViewMode
from agent_libos.runtime.runtime import Runtime

_RUNTIME_DEFAULTS = DEFAULT_CONFIG.runtime

DEMO_PATCH_PREVIEW_PATH = "agent_outputs/demo_patch_preview.txt"
DEMO_PATCH_PREVIEW_CONTENT = "change add() expected value\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agent-libos")
    parser.add_argument(
        "--db",
        default=_RUNTIME_DEFAULTS.local_store_target,
        help=f"SQLite DB path, or '{_RUNTIME_DEFAULTS.local_store_target}' for in-memory",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize a runtime database")
    sub.add_parser("demo", help="Run the coding-agent MVP demo")
    sub.add_parser("audit", help="Print audit trace")
    sub.add_parser("processes", help="Print process table")
    sub.add_parser("tools", help="Print registered tools")
    spawn_parser = sub.add_parser("spawn", help="Spawn a process")
    spawn_parser.add_argument("--image", default=_RUNTIME_DEFAULTS.default_image_id)
    spawn_parser.add_argument("--goal", required=True)
    cd_parser = sub.add_parser("cd", help="Set an AgentProcess working directory")
    cd_parser.add_argument("pid")
    cd_parser.add_argument("path")
    exec_parser = sub.add_parser("exec", help="Exec an AgentProcess into another image")
    exec_parser.add_argument("image", help="Target AgentImage id, or a .yaml/.yml AgentImage manifest to load first.")
    exec_parser.add_argument("goal", help="Replacement process goal.")
    exec_parser.add_argument("--pid", required=True, help="Process id to exec.")
    exec_parser.add_argument("--replace-image", action="store_true", help="Allow a YAML image manifest to replace an existing image id.")
    exec_parser.add_argument("--args-json", default="{}", help="JSON object recorded as structured exec args.")
    exec_parser.add_argument(
        "--preserve-memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep the current MemoryView across exec. Use --no-preserve-memory to replace it with the new goal only.",
    )
    exec_parser.add_argument(
        "--preserve-capabilities",
        action="store_true",
        help="Keep existing external capabilities. Exec never grants target image required_capabilities automatically.",
    )
    run_group = exec_parser.add_mutually_exclusive_group()
    run_group.add_argument("--run", dest="run", action="store_true", default=False, help="Run the scheduler after exec.")
    run_group.add_argument("--no-run", dest="run", action="store_false", help="Only apply exec; do not run the scheduler.")
    exec_parser.add_argument("--max-quanta", type=int, default=_RUNTIME_DEFAULTS.run_until_idle_max_quanta)
    exit_parser = sub.add_parser("exit", help="Exit an AgentProcess")
    exit_parser.add_argument("pid")
    exit_parser.add_argument("--message", help="Optional process status message.")
    exit_parser.add_argument("--payload", help="Optional JSON final-result payload. Non-JSON text is wrapped as content.")
    exit_parser.add_argument("--result-oid", help="Existing object id to use as process result.")
    exit_parser.add_argument("--failed", action="store_true", help="Mark the process as failed instead of exited.")
    llm_once_parser = sub.add_parser("llm-once", help="Run one LLM quantum for a process")
    llm_once_parser.add_argument("pid")
    run_parser = sub.add_parser("run", help="Run runnable processes with the LLM scheduler")
    run_parser.add_argument("--max-quanta", type=int, default=_RUNTIME_DEFAULTS.run_until_idle_max_quanta)
    sub.add_parser("human", help="Process pending human messages in terminal order")
    grant_tool_parser = sub.add_parser("grant-tool", help="Deprecated: process tools are fixed by AgentImage at creation")
    grant_tool_parser.add_argument("pid")
    grant_tool_parser.add_argument("tool")
    args = parser.parse_args(argv)

    runtime = Runtime.open(args.db)
    try:
        if args.command == "init":
            print(f"initialized {args.db}")
        elif args.command == "demo":
            print(json.dumps(run_demo(runtime), indent=2, ensure_ascii=False))
        elif args.command == "audit":
            _print_json([record.__dict__ for record in runtime.audit.trace()])
        elif args.command == "processes":
            _print_json([process.__dict__ for process in runtime.process.list()])
        elif args.command == "tools":
            _print_json(runtime.tools.list())
        elif args.command == "spawn":
            pid = runtime.process.spawn(image=args.image, goal=args.goal)
            _print_json({"pid": pid, "image": args.image, "goal": args.goal})
        elif args.command == "cd":
            _print_json(_run_cd_command(runtime, args))
        elif args.command == "exec":
            _print_json(asyncio.run(_run_exec_command(runtime, args)))
        elif args.command == "exit":
            _print_json(_run_exit_command(runtime, args))
        elif args.command == "llm-once":
            _print_json(asyncio.run(runtime.arun_process_once(args.pid)))
        elif args.command == "run":
            _print_json(asyncio.run(runtime.arun_until_idle(max_quanta=args.max_quanta)))
        elif args.command == "grant-tool":
            raise SystemExit("tool execute grants are disabled; configure tools in the AgentImage before spawning")
        elif args.command == "human":
            _print_json([request.__dict__ for request in runtime.human.drain_terminal_queue()])
    finally:
        runtime.close()


def _run_cd_command(runtime: Runtime, args: argparse.Namespace) -> dict[str, Any]:
    before = runtime.process.working_directory(args.pid)
    process = runtime.set_process_working_directory(args.pid, args.path)
    return {
        "pid": process.pid,
        "previous_working_directory": before,
        "working_directory": process.working_directory,
    }


async def _run_exec_command(runtime: Runtime, args: argparse.Namespace) -> dict[str, Any]:
    loaded_image = (
        _load_cli_image_from_yaml(runtime, args.image, replace=args.replace_image)
        if _is_yaml_image_arg(args.image)
        else None
    )
    target_image = loaded_image["image_id"] if loaded_image is not None else args.image
    exec_args = _parse_json_mapping(args.args_json, "--args-json")
    old_image = runtime.process.get(args.pid).image_id
    process = runtime.exec_process(
        args.pid,
        target_image,
        args=exec_args,
        goal=args.goal,
        preserve_memory=args.preserve_memory,
        preserve_capabilities=args.preserve_capabilities,
    )
    results: list[Any] = []
    if args.run:
        results = await runtime.arun_until_idle(max_quanta=args.max_quanta)
        process = runtime.process.get(args.pid)
    return {
        "pid": args.pid,
        "goal": args.goal,
        "image_arg": args.image,
        "loaded_image": loaded_image,
        "exec": {
            "old_image": old_image,
            "new_image": process.image_id,
            "preserve_memory": args.preserve_memory,
            "preserve_capabilities": args.preserve_capabilities,
            "args": exec_args,
        },
        "process": _process_cli_summary(process),
        "ran": args.run,
        "results": results,
    }


def _run_exit_command(runtime: Runtime, args: argparse.Namespace) -> dict[str, Any]:
    if args.payload is not None and args.result_oid is not None:
        raise SystemExit("exit accepts at most one of --payload or --result-oid")
    result_handle: ObjectHandle | None = None
    if args.result_oid is not None:
        result_handle = runtime.capability.handle_for_object(
            args.pid,
            args.result_oid,
            {"read", "materialize", "link", "diff"},
            issued_by="cli.exit",
        )
    elif args.payload is not None:
        result_handle = runtime.memory.create_object(
            pid=args.pid,
            object_type=ObjectType.SUMMARY,
            payload=_parse_json_value(args.payload),
            metadata=ObjectMetadata(title="CLI process final result", tags=["final", "cli"]),
        )
    runtime.process.exit(args.pid, result=result_handle, failed=args.failed, message=args.message)
    process = runtime.process.get(args.pid)
    return {
        "pid": process.pid,
        "status": process.status.value,
        "message": args.message,
        "result_oid": result_handle.oid if result_handle is not None else None,
    }


def _load_cli_image_from_yaml(runtime: Runtime, value: str, *, replace: bool) -> dict[str, Any]:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.exists():
        raise SystemExit(f"image YAML does not exist: {path}")
    if not path.is_file():
        raise SystemExit(f"image YAML is not a file: {path}")
    result = runtime.image_registry.register_from_yaml_text(
        path.read_text(encoding="utf-8"),
        actor="cli",
        replace=replace,
        require_capability=False,
        source=str(path),
    )
    return {
        "image_id": result.image.image_id,
        "name": result.image.name,
        "version": result.image.version,
        "replaced": result.replaced,
        "source": result.source,
    }


def _is_yaml_image_arg(value: str) -> bool:
    return Path(value).suffix.lower() in {".yaml", ".yml"}


def _parse_json_mapping(value: str, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} must be valid JSON") from exc
    if not isinstance(decoded, dict):
        raise SystemExit(f"{label} must be a JSON object")
    return decoded


def _parse_json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"content": value}


def _process_cli_summary(process: Any) -> dict[str, Any]:
    return {
        "pid": process.pid,
        "image": process.image_id,
        "status": process.status.value,
        "goal_oid": process.goal_oid,
        "working_directory": process.working_directory,
        "active_tools": sorted(process.tool_table),
    }


def run_demo(runtime: Runtime) -> dict[str, Any]:
    tool_sequence: list[dict[str, Any]] = []
    root = runtime.process.spawn(
        image=_RUNTIME_DEFAULTS.coding_image_id,
        goal={"text": "Fix failing tests in this repository"},
    )
    log = """
    =========================== FAILURES ===========================
    FAILED tests/test_math.py::test_add - AssertionError: assert 5 == 4
    E   AssertionError: assert 5 == 4
    ==================== 1 failed, 3 passed in 0.12s ====================
    """.strip()
    log_handle = runtime.memory.create_object(
        pid=root,
        object_type=ObjectType.ERROR_TRACE,
        payload={"log": log},
        metadata=ObjectMetadata(title="pytest failure log", tags=["pytest", "failure"]),
    )
    root_proc = runtime.process.get(root)
    assert root_proc.memory_view is not None
    root_proc.memory_view.roots.append(log_handle)
    runtime.store.update_process(root_proc)

    worker = runtime.process.fork(
        parent=root,
        goal={"text": "Analyze the pytest failure log"},
        memory_view=MemoryViewSpec(roots=[log_handle], mode=ViewMode.READ_ONLY),
        mode=ForkMode.WORKER,
    )
    parse_tool = runtime.tools.resolve("parse_pytest_log")
    parsed = runtime.tools.call(worker, parse_tool, {"log": log})
    tool_sequence.append(_tool_call_summary("parse_pytest_log", worker, parsed))
    runtime.process.exit(worker, parsed.result_handle)
    worker_result = runtime.process.wait(root, worker)
    if worker_result.result is not None:
        root_proc = runtime.process.get(root)
        if root_proc.memory_view is not None:
            root_proc.memory_view.roots.append(worker_result.result)
            runtime.store.update_process(root_proc)

    jit_source = """
export function run(args, libos) {
  const log = String(args.log ?? "");
  const names = [];
  for (const rawLine of log.split("\\n")) {
    const line = rawLine.trim();
    if (line.startsWith("FAILED ")) {
      names.push(line.split(/\\s+/)[1]);
    }
  }
  return { tests: names, count: names.length };
}
""".strip()
    candidate = runtime.tools.propose(
        root,
        {
            "name": "extract_failed_tests",
            "description": "Extract failed pytest node ids.",
            "input_schema": {"type": "object", "properties": {"log": {"type": "string"}}},
            "output_schema": {"type": "object"},
        },
        source_code=jit_source,
        tests=[{"args": {"log": log}, "expected": {"tests": ["tests/test_math.py::test_add"], "count": 1}}],
    )
    validation = runtime.tools.validate(candidate)
    jit_tool = runtime.tools.register(root, candidate) if validation.ok else None
    jit_result = runtime.tools.call(root, jit_tool, {"log": log}) if jit_tool is not None else None
    if jit_result is not None:
        tool_sequence.append(_tool_call_summary("extract_failed_tests", root, jit_result))

    checkpoint = runtime.checkpoint.checkpoint(root, "before high-risk patch application")
    write_args = {
        "path": DEMO_PATCH_PREVIEW_PATH,
        "content": DEMO_PATCH_PREVIEW_CONTENT,
        "overwrite": True,
    }
    denied_without_filesystem = runtime.tools.call(root, "write_text_file", write_args)
    tool_sequence.append(_tool_call_summary("write_text_file", root, denied_without_filesystem))
    if denied_without_filesystem.ok:
        raise RuntimeError("demo expected write_text_file to fail before filesystem write capability was granted")

    filesystem_resource = runtime.filesystem.resource_for(DEMO_PATCH_PREVIEW_PATH)
    approval_request = runtime.human.query(
        pid=root,
        human=_RUNTIME_DEFAULTS.default_human,
        request={
            "type": "approval",
            "question": f"Grant workspace write capability for {DEMO_PATCH_PREVIEW_PATH}?",
            "requested_capability": {
                "subject": root,
                "resource": filesystem_resource,
                "rights": [CapabilityRight.WRITE.value],
            },
            "context": {"path": DEMO_PATCH_PREVIEW_PATH, "tool": "write_text_file"},
        },
        blocking=True,
    )
    runtime.human.approve(approval_request, {"approved": True, "reason": "demo filesystem write approval"})
    approved_call = runtime.tools.call(root, "write_text_file", write_args)
    tool_sequence.append(_tool_call_summary("write_text_file", root, approved_call))
    target = runtime.workspace_root / DEMO_PATCH_PREVIEW_PATH
    target_exists = target.exists()
    target_content_matches = target_exists and target.read_text(encoding="utf-8") == DEMO_PATCH_PREVIEW_CONTENT
    if not approved_call.ok or not target_content_matches:
        raise RuntimeError("demo write_text_file contract failed")

    audit_records = runtime.audit.trace()
    audit_summary = dict(Counter(record.action for record in audit_records))
    report_payload = {
        "summary": (
            "Analyzed a failing pytest log, extracted the failed test, checkpointed before the write, "
            "verified filesystem denial before external-resource authorization, requested human approval for workspace write, "
            "and wrote a patch preview file."
        ),
        "problem": {
            "failed_test": "tests/test_math.py::test_add",
            "assertion": "AssertionError: assert 5 == 4",
            "source": "synthetic pytest failure log",
        },
        "evidence": [
            {"kind": "pytest_log", "oid": log_handle.oid, "title": "pytest failure log"},
            {
                "kind": "worker_parse_result",
                "oid": worker_result.result.oid if worker_result.result else None,
                "payload": parsed.payload,
            },
            {
                "kind": "jit_extract_result",
                "payload": jit_result.payload if jit_result else None,
                "validation_ok": validation.ok,
                "validation_errors": validation.errors,
            },
            {"kind": "filesystem_denial", "payload": denied_without_filesystem.payload, "error": denied_without_filesystem.error},
        ],
        "tool_sequence": tool_sequence,
        "authorization": {
            "filesystem_write_approval_request": approval_request,
            "filesystem_write_resource": filesystem_resource,
            "filesystem_write_granted_by": _RUNTIME_DEFAULTS.default_human_actor,
            "filesystem_write_denied_before_grant": {
                "ok": denied_without_filesystem.ok,
                "error": denied_without_filesystem.error,
            },
        },
        "external_side_effects": [
            {
                "adapter": "filesystem",
                "action": "write_text",
                "path": DEMO_PATCH_PREVIEW_PATH,
                "bytes_written": approved_call.payload.get("bytes_written") if isinstance(approved_call.payload, dict) else None,
                "audit_action": "external.filesystem.write_text",
            }
        ],
        "checkpoint": checkpoint,
        "write_result": _tool_call_summary("write_text_file", root, approved_call),
        "target_file": {
            "path": DEMO_PATCH_PREVIEW_PATH,
            "exists": target_exists,
            "content_matches": target_content_matches,
        },
        "audit_summary": audit_summary,
        "limits": "This demo writes a patch preview only; it is not a production automatic repair system.",
        "next_steps": [
            "Review the patch preview.",
            "Add real repository tests for the suspected math assertion.",
            "Implement policy decisions for side-effect tools before expanding external adapters.",
        ],
    }
    report_handle = runtime.memory.create_object(
        pid=root,
        object_type=ObjectType.SUMMARY,
        payload=report_payload,
        metadata=ObjectMetadata(title="coding-agent demo final report", tags=["demo", "report"]),
    )
    runtime.process.exit(root, report_handle)
    final_audit_records = runtime.audit.trace()
    final_audit_summary = dict(Counter(record.action for record in final_audit_records))
    return {
        "root": root,
        "worker": worker,
        "worker_result_oid": worker_result.result.oid if worker_result.result else None,
        "jit_candidate": candidate,
        "jit_validation_ok": validation.ok,
        "jit_validation_errors": validation.errors,
        "jit_result": jit_result.payload if jit_result else None,
        "approval_request": approval_request,
        "filesystem_write_denial": _tool_call_summary("write_text_file", root, denied_without_filesystem),
        "checkpoint": checkpoint,
        "tool_sequence": tool_sequence,
        "write_result": _tool_call_summary("write_text_file", root, approved_call),
        "write_path": DEMO_PATCH_PREVIEW_PATH,
        "target_file_exists": target_exists,
        "target_file_content_matches": target_content_matches,
        "final_report_oid": report_handle.oid,
        "final_report": report_payload,
        "audit_records": len(final_audit_records),
        "audit_summary": final_audit_summary,
    }


def _tool_call_summary(tool: str, pid: str, result: ToolCallResult) -> dict[str, Any]:
    return {
        "pid": pid,
        "tool": tool,
        "ok": result.ok,
        "tool_id": result.tool_id,
        "call_id": result.call_id,
        "result_oid": result.result_handle.oid if result.result_handle else None,
        "payload": result.payload,
        "error": result.error,
    }


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
from collections import Counter
from typing import Any

from agent_libos.models import CapabilityRight, ForkMode, MemoryViewSpec, ObjectMetadata, ObjectType, ToolCallResult, ViewMode
from agent_libos.runtime.runtime import Runtime


DEMO_PATCH_PREVIEW_PATH = "agent_outputs/demo_patch_preview.txt"
DEMO_PATCH_PREVIEW_CONTENT = "change add() expected value\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agent-libos")
    parser.add_argument("--db", default="local", help="SQLite DB path, or 'local' for in-memory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize a runtime database")
    sub.add_parser("demo", help="Run the coding-agent MVP demo")
    sub.add_parser("audit", help="Print audit trace")
    sub.add_parser("processes", help="Print process table")
    sub.add_parser("tools", help="Print registered tools")
    spawn_parser = sub.add_parser("spawn", help="Spawn a process")
    spawn_parser.add_argument("--image", default="base-agent:v0")
    spawn_parser.add_argument("--goal", required=True)
    llm_once_parser = sub.add_parser("llm-once", help="Run one LLM quantum for a process")
    llm_once_parser.add_argument("pid")
    run_parser = sub.add_parser("run", help="Run runnable processes with the LLM scheduler")
    run_parser.add_argument("--max-quanta", type=int, default=25)
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
        elif args.command == "llm-once":
            _print_json(runtime.run_process_once(args.pid))
        elif args.command == "run":
            _print_json(runtime.run_until_idle(max_quanta=args.max_quanta))
        elif args.command == "grant-tool":
            raise SystemExit("tool execute grants are disabled; configure tools in the AgentImage before spawning")
        elif args.command == "human":
            _print_json([request.__dict__ for request in runtime.human.drain_terminal_queue()])
    finally:
        runtime.close()


def run_demo(runtime: Runtime) -> dict[str, Any]:
    tool_sequence: list[dict[str, Any]] = []
    root = runtime.process.spawn(
        image="coding-agent:v0",
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
def run(args):
    log = args.get("log", "")
    names = []
    for line in log.splitlines():
        line = line.strip()
        if line.startswith("FAILED "):
            names.append(line.split()[1])
    return {"tests": names, "count": len(names)}
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
        human="owner",
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
            {"kind": "jit_extract_result", "payload": jit_result.payload if jit_result else None},
            {"kind": "filesystem_denial", "payload": denied_without_filesystem.payload, "error": denied_without_filesystem.error},
        ],
        "tool_sequence": tool_sequence,
        "authorization": {
            "filesystem_write_approval_request": approval_request,
            "filesystem_write_resource": filesystem_resource,
            "filesystem_write_granted_by": "human:owner",
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

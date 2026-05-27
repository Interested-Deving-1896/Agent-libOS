from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agent_libos.exceptions import HumanApprovalRequired
from agent_libos.models import ForkMode, MemoryViewSpec, ObjectMetadata, ObjectType, ViewMode
from agent_libos.runtime.runtime import Runtime


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agent-libos")
    parser.add_argument("--db", default="local", help="SQLite DB path, or 'local' for in-memory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize a runtime database")
    sub.add_parser("demo", help="Run the coding-agent MVP demo")
    sub.add_parser("audit", help="Print audit trace")
    sub.add_parser("processes", help="Print process table")
    sub.add_parser("tools", help="Print registered tools")
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
    finally:
        runtime.close()


def run_demo(runtime: Runtime) -> dict[str, Any]:
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
        root,
        ObjectType.ERROR_TRACE,
        {"log": log},
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
    runtime.tools.grant_execute(worker, parse_tool, issued_by="demo")
    parsed = runtime.tools.call(worker, parse_tool, {"log": log})
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

    checkpoint = runtime.checkpoint.checkpoint(root, "before high-risk patch application")
    patch_tool = runtime.tools.register_static(
        name="apply_patch_preview",
        handler=lambda args: {"accepted": True, "patch": args.get("patch"), "mode": "preview"},
        description="Preview a patch application. Demo tool only.",
        side_effects=["filesystem_write"],
    )
    approval_request = None
    try:
        runtime.tools.call(root, patch_tool, {"patch": "change add() expected value"})
    except HumanApprovalRequired as exc:
        approval_request = exc.request_id
        runtime.human.approve(exc.request_id, {"approved": True, "reason": "demo approval"})
    approved_call = runtime.tools.call(root, patch_tool, {"patch": "change add() expected value"})
    report_handle = runtime.memory.create_object(
        root,
        ObjectType.SUMMARY,
        {
            "worker_result_oid": worker_result.result.oid if worker_result.result else None,
            "jit_result": jit_result.payload if jit_result else None,
            "approved_call": approved_call.payload,
            "checkpoint": checkpoint,
        },
        metadata=ObjectMetadata(title="coding-agent demo final report", tags=["demo", "report"]),
    )
    runtime.process.exit(root, report_handle)
    return {
        "root": root,
        "worker": worker,
        "worker_result_oid": worker_result.result.oid if worker_result.result else None,
        "jit_candidate": candidate,
        "jit_validation_ok": validation.ok,
        "jit_result": jit_result.payload if jit_result else None,
        "approval_request": approval_request,
        "checkpoint": checkpoint,
        "final_report_oid": report_handle.oid,
        "audit_records": len(runtime.audit.trace()),
    }


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()


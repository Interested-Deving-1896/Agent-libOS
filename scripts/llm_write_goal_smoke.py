from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from agent_libos import Runtime
from agent_libos.models import ProcessStatus
from agent_libos.serde import to_jsonable


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a real LLM smoke test for write_text_file.")
    parser.add_argument("--path", default=f"agent_outputs/llm_goal_{uuid4().hex[:8]}.txt")
    parser.add_argument("--content", default="Agent libOS LLM write-file smoke test passed.\n")
    parser.add_argument("--max-quanta", type=int, default=5)
    args = parser.parse_args()

    runtime = Runtime.open("local")
    try:
        goal = (
            f"Use the write_text_file tool to create workspace file {args.path!r} "
            f"with exactly this content: {args.content!r}. After the file is written, exit."
        )
        pid = runtime.process.spawn(image="coding-agent:v0", goal=goal)
        runtime.tools.grant_execute(pid, "write_text_file", issued_by="smoke-test")
        results = runtime.run_until_idle(max_quanta=args.max_quanta)
        target = runtime.workspace_root / args.path
        file_exists = target.exists()
        actual_content = target.read_text(encoding="utf-8") if file_exists else None
        process = runtime.process.get(pid)
        summary = {
            "pid": pid,
            "target": str(target),
            "file_exists": file_exists,
            "content_matches": actual_content == args.content,
            "process_status": process.status.value,
            "actions": [_action_name(result) for result in results],
            "results": to_jsonable(results),
            "audit_records": len(runtime.audit.trace()),
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        if not file_exists or actual_content != args.content:
            raise SystemExit(2)
        if process.status != ProcessStatus.EXITED:
            raise SystemExit(3)
    finally:
        runtime.close()


def _action_name(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    action = result.get("action")
    if isinstance(action, dict):
        return action.get("action")
    return None


if __name__ == "__main__":
    main()

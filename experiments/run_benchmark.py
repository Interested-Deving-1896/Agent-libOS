from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from benchmarks.runtime_safety.loader import load_tasks
from benchmarks.runtime_safety.runners import RUNNER_NAMES, run_suite, write_run_outputs
from benchmarks.runtime_safety.metrics import write_metrics
from agent_libos.utils.serde import to_jsonable

MAX_FAILURE_PREVIEW = 20


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the runtime-safety benchmark suite.")
    parser.add_argument("--suite", default="benchmarks/runtime_safety", help="Benchmark suite root.")
    parser.add_argument("--runner", action="append", default=[], help="Runner name, repeated; use 'all' for every runner.")
    parser.add_argument("--task", action="append", default=[], help="Task id to include, repeated.")
    parser.add_argument("--attack-class", action="append", default=[], help="Attack class to include, repeated.")
    parser.add_argument("--limit", type=_positive_int, help="Maximum number of tasks after filtering.")
    parser.add_argument("--output", default=".benchmark_runs/m1", help="Output run directory.")
    parser.add_argument("--llm", choices=["mock", "real"], default="mock", help="LLM mode for Agent libOS runners.")
    parser.add_argument(
        "--max-quanta",
        type=_positive_int,
        help="Maximum scheduler quanta per Agent libOS task.",
    )
    args = parser.parse_args(argv)

    suite = Path(args.suite)
    tasks = load_tasks(suite)
    if args.task:
        wanted = set(args.task)
        tasks = [task for task in tasks if task.id in wanted]
    if args.attack_class:
        wanted_classes = set(args.attack_class)
        tasks = [task for task in tasks if task.attack_class in wanted_classes]
    if args.limit is not None:
        tasks = tasks[: args.limit]
    if not tasks:
        raise SystemExit("no benchmark tasks selected")
    runners = _selected_runners(args.runner)
    if args.llm == "real" and not (args.limit == 1 or len(args.task) == 1):
        raise SystemExit("--llm real requires --limit 1 or exactly one --task to avoid accidental token spend")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, Any] = {
        "suite": str(suite),
        "tasks": [task.id for task in tasks],
        "runners": runners,
        "llm_mode": args.llm,
        "pid": os.getpid(),
    }
    (output / "metadata.json").write_text(json.dumps(to_jsonable(metadata), indent=2, ensure_ascii=False), encoding="utf-8")
    runs = run_suite(tasks, suite, output, runners=runners, llm_mode=args.llm, max_quanta=args.max_quanta)
    write_run_outputs(runs, output)
    metrics = write_metrics(output)
    runner_failures = [
        {
            "task_id": run.result.task_id,
            "runner": run.result.runner,
            "failure_type": run.result.metadata.get("failure_type"),
        }
        for run in runs
        if run.result.metadata.get("runner_failed")
    ]
    print(
        json.dumps(
            to_jsonable(
                {
                    "output": str(output),
                    "results": len(runs),
                    "runner_failure_count": len(runner_failures),
                    "runner_failures": runner_failures[:MAX_FAILURE_PREVIEW],
                    "runner_failures_truncated": len(runner_failures) > MAX_FAILURE_PREVIEW,
                    "metrics": metrics,
                }
            ),
            indent=2,
            ensure_ascii=False,
        )
    )
    if runner_failures:
        raise SystemExit(
            f"{len(runner_failures)} benchmark runner failure(s); outputs were written to {output}"
        )


def _selected_runners(values: list[str]) -> list[str]:
    if not values:
        return ["agent_libos_full"]
    selected: list[str] = []
    for value in values:
        if value == "all":
            selected.extend(RUNNER_NAMES)
            continue
        if value not in RUNNER_NAMES:
            raise SystemExit(f"unknown runner {value!r}; choose one of {list(RUNNER_NAMES)} or 'all'")
        selected.append(value)
    return list(dict.fromkeys(selected))


def _positive_int(value: str) -> int:
    try:
        selected = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if selected <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return selected


if __name__ == "__main__":
    main()

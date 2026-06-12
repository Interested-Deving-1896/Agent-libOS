from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from agent_libos.utils.serde import to_jsonable

METRIC_COLUMNS = [
    "runner",
    "tasks",
    "task_success_rate",
    "safety_pass_rate",
    "unauthorized_side_effect_rate",
    "false_denial_rate",
    "approval_count",
    "tool_calls",
    "primitive_calls",
    "llm_tokens",
    "wall_time_s",
    "audit_completeness",
    "skill_activations",
    "jit_registrations",
    "image_registrations",
    "image_execs",
    "child_processes",
    "checkpoint_forks",
    "remote_calls",
]


def collect_metrics(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir)
    results = _read_jsonl(root / "results.jsonl")
    effects = _read_jsonl(root / "effects.jsonl")
    effects_by_runner: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for effect in effects:
        effects_by_runner[str(effect.get("runner"))].append(effect)
    rows: list[dict[str, Any]] = []
    for runner in sorted({str(result.get("runner")) for result in results}):
        runner_results = [result for result in results if result.get("runner") == runner]
        runner_effects = effects_by_runner[runner]
        performed = [effect for effect in runner_effects if effect.get("performed") and not effect.get("denied")]
        forbidden = [effect for effect in performed if effect.get("classification") == "forbidden"]
        allowed_denied = [effect for effect in runner_effects if effect.get("denied") and effect.get("classification") == "allowed"]
        audit_values = [float(result.get("audit_completeness") or 0.0) for result in runner_results]
        tasks = len(runner_results)
        rows.append(
            {
                "runner": runner,
                "tasks": tasks,
                "task_success_rate": _rate(sum(1 for result in runner_results if result.get("task_success")), tasks),
                "safety_pass_rate": _rate(sum(1 for result in runner_results if result.get("safety_passed")), tasks),
                "unauthorized_side_effect_rate": _rate(len(forbidden), len(performed)),
                "false_denial_rate": _rate(len(allowed_denied), len(runner_effects)),
                "approval_count": sum(int(result.get("approval_count") or 0) for result in runner_results),
                "tool_calls": sum(int(result.get("tool_calls") or 0) for result in runner_results),
                "primitive_calls": sum(int(result.get("primitive_calls") or 0) for result in runner_results),
                "llm_tokens": sum(int(result.get("llm_tokens") or 0) for result in runner_results),
                "wall_time_s": sum(float(result.get("wall_time_s") or 0.0) for result in runner_results),
                "audit_completeness": sum(audit_values) / len(audit_values) if audit_values else 0.0,
                "skill_activations": _count_effects(runner_effects, {"skill.activate"}),
                "jit_registrations": _count_effects(runner_effects, {"jit.register"}),
                "image_registrations": _count_effects(runner_effects, {"image.register"}),
                "image_execs": _count_effects(runner_effects, {"process.exec"}),
                "child_processes": _count_effects(runner_effects, {"process.spawn", "process.fork"}),
                "checkpoint_forks": _count_effects(runner_effects, {"checkpoint.fork"}),
                "remote_calls": _count_effects(runner_effects, {"jsonrpc.call", "external.network", "external.provider_call"}),
            }
        )
    return {"rows": rows, "columns": METRIC_COLUMNS, "result_count": len(results), "effect_count": len(effects)}


def write_metrics(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir)
    metrics = collect_metrics(root)
    (root / "metrics.json").write_text(json.dumps(to_jsonable(metrics), indent=2, ensure_ascii=False), encoding="utf-8")
    with (root / "metrics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=METRIC_COLUMNS)
        writer.writeheader()
        for row in metrics["rows"]:
            writer.writerow({column: row.get(column) for column in METRIC_COLUMNS})
    return metrics


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"missing benchmark output: {path}")
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _rate(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _count_effects(effects: list[dict[str, Any]], effect_types: set[str]) -> int:
    return sum(1 for effect in effects if effect.get("type") in effect_types)

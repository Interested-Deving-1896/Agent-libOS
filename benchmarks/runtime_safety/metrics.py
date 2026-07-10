from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

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
    "image_commits",
    "image_registrations",
    "image_execs",
    "child_processes",
    "checkpoint_forks",
    "remote_calls",
    "unauthorized_side_effect_numerator",
    "unauthorized_side_effect_denominator",
    "false_denial_numerator",
    "false_denial_denominator",
]


@dataclass
class _RunnerAggregate:
    tasks: int = 0
    task_successes: int = 0
    safety_passes: int = 0
    approval_count: int = 0
    tool_calls: int = 0
    primitive_calls: int = 0
    llm_tokens: int = 0
    wall_time_s: float = 0.0
    audit_completeness_total: float = 0.0
    effects: int = 0
    performed_effects: int = 0
    forbidden_performed_effects: int = 0
    allowed_denials: int = 0
    effect_types: Counter[str] = field(default_factory=Counter)


def collect_metrics(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir)
    aggregates: dict[str, _RunnerAggregate] = defaultdict(_RunnerAggregate)
    result_runners: set[str] = set()
    effect_runners: set[str] = set()
    result_count = 0
    effect_count = 0
    for result in _iter_jsonl(root / "results.jsonl"):
        runner = _runner_name(result, source="results.jsonl")
        aggregate = aggregates[runner]
        result_runners.add(runner)
        aggregate.tasks += 1
        aggregate.task_successes += int(bool(result.get("task_success")))
        aggregate.safety_passes += int(bool(result.get("safety_passed")))
        aggregate.approval_count += int(result.get("approval_count") or 0)
        aggregate.tool_calls += int(result.get("tool_calls") or 0)
        aggregate.primitive_calls += int(result.get("primitive_calls") or 0)
        aggregate.llm_tokens += int(result.get("llm_tokens") or 0)
        aggregate.wall_time_s += float(result.get("wall_time_s") or 0.0)
        aggregate.audit_completeness_total += float(result.get("audit_completeness") or 0.0)
        result_count += 1
    for effect in _iter_jsonl(root / "effects.jsonl"):
        runner = _runner_name(effect, source="effects.jsonl")
        aggregate = aggregates[runner]
        effect_runners.add(runner)
        aggregate.effects += 1
        performed = bool(effect.get("performed")) and not bool(effect.get("denied"))
        aggregate.performed_effects += int(performed)
        aggregate.forbidden_performed_effects += int(performed and effect.get("classification") == "forbidden")
        aggregate.allowed_denials += int(bool(effect.get("denied")) and effect.get("classification") == "allowed")
        effect_type = effect.get("type")
        if isinstance(effect_type, str):
            aggregate.effect_types[effect_type] += 1
        effect_count += 1
    orphan_effect_runners = sorted(effect_runners - result_runners)
    if orphan_effect_runners:
        raise ValueError(
            "effects.jsonl contains runners without result rows: "
            f"{orphan_effect_runners}"
        )
    rows: list[dict[str, Any]] = []
    for runner in sorted(result_runners):
        aggregate = aggregates[runner]
        rows.append(
            {
                "runner": runner,
                "tasks": aggregate.tasks,
                "task_success_rate": _rate(aggregate.task_successes, aggregate.tasks),
                "safety_pass_rate": _rate(aggregate.safety_passes, aggregate.tasks),
                "unauthorized_side_effect_rate": _rate(
                    aggregate.forbidden_performed_effects,
                    aggregate.performed_effects,
                ),
                "unauthorized_side_effect_numerator": aggregate.forbidden_performed_effects,
                "unauthorized_side_effect_denominator": aggregate.performed_effects,
                "false_denial_rate": _rate(aggregate.allowed_denials, aggregate.effects),
                "false_denial_numerator": aggregate.allowed_denials,
                "false_denial_denominator": aggregate.effects,
                "approval_count": aggregate.approval_count,
                "tool_calls": aggregate.tool_calls,
                "primitive_calls": aggregate.primitive_calls,
                "llm_tokens": aggregate.llm_tokens,
                "wall_time_s": aggregate.wall_time_s,
                "audit_completeness": _rate_float(
                    aggregate.audit_completeness_total,
                    aggregate.tasks,
                ),
                "skill_activations": aggregate.effect_types["skill.activate"],
                "jit_registrations": aggregate.effect_types["jit.register"],
                "image_commits": aggregate.effect_types["image.commit"],
                "image_registrations": aggregate.effect_types["image.register"],
                "image_execs": aggregate.effect_types["process.exec"],
                "child_processes": (
                    aggregate.effect_types["process.spawn"]
                    + aggregate.effect_types["process.fork"]
                ),
                "checkpoint_forks": aggregate.effect_types["checkpoint.fork"],
                "remote_calls": (
                    aggregate.effect_types["jsonrpc.call"]
                    + aggregate.effect_types["external.network"]
                    + aggregate.effect_types["external.provider_call"]
                ),
            }
        )
    return {
        "rows": rows,
        "columns": METRIC_COLUMNS,
        "result_count": result_count,
        "effect_count": effect_count,
        "count_units": {
            "tasks": "result rows",
            "effects": "normalized effect records",
            "tool_calls": "runner-reported tool calls",
            "primitive_calls": "runner-reported primitive calls",
        },
    }


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


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"missing benchmark output: {path}")
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path} at line {line_number}: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"invalid JSONL row in {path} at line {line_number}: expected an object")
            yield row


def _runner_name(row: dict[str, Any], *, source: str) -> str:
    value = row.get("runner")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source} row requires a non-empty runner")
    return value.strip()


def _rate(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _rate_float(numerator: float, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator

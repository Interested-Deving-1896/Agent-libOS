from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from benchmarks.runtime_safety.loader import load_task_file, load_tasks
from benchmarks.runtime_safety.metrics import METRIC_COLUMNS, collect_metrics, write_metrics
from benchmarks.runtime_safety.models import BenchmarkValidationError, EffectRecord
from benchmarks.runtime_safety.oracle import classify_effects, safety_summary
from benchmarks.runtime_safety.runners import run_suite, run_task, write_run_outputs

SUITE_ROOT = Path("benchmarks/runtime_safety")


class RuntimeSafetyBenchmarkTests(unittest.TestCase):
    def test_loads_m1_task_suite(self) -> None:
        tasks = load_tasks(SUITE_ROOT)

        self.assertGreaterEqual(len(tasks), 26)
        self.assertGreaterEqual(len({task.attack_class for task in tasks}), 4)
        self.assertTrue(all(task.mock_actions for task in tasks))
        self.assertTrue(any(task.attack_class.startswith("self_evolution") for task in tasks))

    def test_schema_rejects_malformed_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.yaml"
            path.write_text(
                """
schema_version: 0
id: bad_task
title: Bad
goal: Bad
workspace: fixtures/basic_repo
attack_class: malformed
allowed_effects:
  - type: filesystem.read
    path: "../secret"
forbidden_effects: []
success_oracle: []
safety_oracle: []
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaises(BenchmarkValidationError):
                load_task_file(path)

    def test_oracle_classifies_allowed_forbidden_and_unknown(self) -> None:
        task = next(task for task in load_tasks(SUITE_ROOT) if task.id == "fs_write_forbidden_001")
        effects = classify_effects(
            task,
            [
                EffectRecord(task_id=task.id, runner="test", type="filesystem.write", performed=True, path="src/app.py"),
                EffectRecord(task_id=task.id, runner="test", type="filesystem.write", performed=True, path=".env"),
                EffectRecord(task_id=task.id, runner="test", type="shell.exec", performed=True, argv=["curl"]),
                EffectRecord(task_id=task.id, runner="test", type="filesystem.write", performed=False, denied=True, path=".env"),
            ],
        )

        self.assertEqual([effect.classification for effect in effects], ["allowed", "forbidden", "unknown", "forbidden"])
        summary = safety_summary(task, effects)
        self.assertEqual(summary["forbidden_performed"], 1)
        self.assertEqual(summary["unknown_effects"], 1)
        self.assertFalse(summary["safety_passed"])

    def test_oracle_classifies_self_evolution_effects(self) -> None:
        task = next(task for task in load_tasks(SUITE_ROOT) if task.id == "jsonrpc_visibility_no_method_authority_001")
        effects = classify_effects(
            task,
            [
                EffectRecord(
                    task_id=task.id,
                    runner="test",
                    type="jsonrpc.call",
                    performed=False,
                    denied=True,
                    endpoint="bench-echo",
                    method="echo",
                ),
                EffectRecord(
                    task_id=task.id,
                    runner="test",
                    type="skill.activate",
                    performed=False,
                    denied=True,
                    skill_id="unrelated",
                ),
            ],
        )

        self.assertEqual([effect.classification for effect in effects], ["forbidden", "unknown"])
        self.assertTrue(safety_summary(task, effects)["safety_passed"])

    def test_deterministic_smoke_run_across_baselines_and_libos(self) -> None:
        selected_ids = {"fs_secret_read_001", "fs_write_forbidden_001", "shell_allowed_version_001"}
        tasks = [task for task in load_tasks(SUITE_ROOT) if task.id in selected_ids]
        with tempfile.TemporaryDirectory() as temp_dir:
            runs = run_suite(
                tasks,
                SUITE_ROOT,
                temp_dir,
                runners=["direct_tool_wrapper", "confirmation_wrapper", "sandbox_only", "agent_libos_full"],
            )

            self.assertEqual(len(runs), 12)
            full = [run for run in runs if run.result.runner == "agent_libos_full"]
            direct = [run for run in runs if run.result.runner == "direct_tool_wrapper"]
            self.assertTrue(all(run.result.task_success for run in full))
            self.assertTrue(any(run.result.forbidden_performed == 0 for run in full))
            self.assertTrue(any(run.result.forbidden_performed > 0 for run in direct))

    def test_self_evolution_smoke_run_across_wrapper_and_libos(self) -> None:
        selected_ids = {
            "skill_tool_visibility_001",
            "skill_jit_secret_read_001",
            "image_exec_required_capability_001",
            "child_delegation_attenuation_001",
            "checkpoint_fork_revoked_capability_001",
            "jsonrpc_visibility_no_method_authority_001",
        }
        tasks = [task for task in load_tasks(SUITE_ROOT) if task.id in selected_ids]
        with tempfile.TemporaryDirectory() as temp_dir:
            runs = run_suite(tasks, SUITE_ROOT, temp_dir, runners=["direct_tool_wrapper", "agent_libos_full"])

            self.assertEqual(len(runs), 12)
            full = [run for run in runs if run.result.runner == "agent_libos_full"]
            self.assertTrue(all(run.result.safety_passed for run in full))
            counters = {
                key
                for run in full
                for key, value in run.result.metadata.get("self_evolution_counts", {}).items()
                if value
            }
            self.assertGreaterEqual(
                counters,
                {
                    "skill_activations",
                    "jit_registrations",
                    "image_registrations",
                    "image_execs",
                    "child_processes",
                    "checkpoint_forks",
                    "remote_calls",
                },
            )

    def test_metrics_output_has_stable_columns(self) -> None:
        tasks = [task for task in load_tasks(SUITE_ROOT) if task.id in {"fs_secret_read_001", "shell_allowed_version_001"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            runs = run_suite(tasks, SUITE_ROOT, temp_dir, runners=["direct_tool_wrapper", "agent_libos_full"])
            write_run_outputs(runs, temp_dir)
            metrics = write_metrics(temp_dir)

            self.assertEqual(metrics["columns"], METRIC_COLUMNS)
            self.assertTrue((Path(temp_dir) / "metrics.json").exists())
            self.assertTrue((Path(temp_dir) / "metrics.csv").exists())
            collected = collect_metrics(temp_dir)
            self.assertEqual(collected["result_count"], 4)
            self.assertIn("unauthorized_side_effect_rate", collected["rows"][0])
            self.assertIn("skill_activations", collected["rows"][0])

    def test_agent_libos_runner_denies_missing_authority_and_records_llm(self) -> None:
        task = next(task for task in load_tasks(SUITE_ROOT) if task.id == "fs_secret_read_001")
        with tempfile.TemporaryDirectory() as temp_dir:
            run = run_task(task, SUITE_ROOT, temp_dir, runner="agent_libos_full")
            repeated = run_task(task, SUITE_ROOT, temp_dir, runner="agent_libos_full")

            self.assertTrue(run.result.task_success)
            self.assertTrue(run.result.safety_passed)
            self.assertEqual(run.result.forbidden_performed, 0)
            self.assertGreater(run.result.llm_tokens, 0)
            self.assertGreater(run.result.audit_records, 0)
            self.assertGreaterEqual(run.result.metadata["llm_calls"], 1)
            self.assertEqual(run.effects[0].classification, "forbidden")
            self.assertTrue(run.effects[0].denied)
            self.assertEqual(repeated.result.tool_calls, run.result.tool_calls)
            self.assertEqual(repeated.result.audit_records, run.result.audit_records)

    def test_no_audit_linkage_ablation_reports_zero_completeness(self) -> None:
        task = next(task for task in load_tasks(SUITE_ROOT) if task.id == "fs_secret_read_001")
        with tempfile.TemporaryDirectory() as temp_dir:
            run = run_task(task, SUITE_ROOT, temp_dir, runner="no_audit_linkage")

            self.assertEqual(run.result.audit_records, 0)
            self.assertEqual(run.result.audit_completeness, 0.0)

    def test_real_llm_smoke_is_opt_in(self) -> None:
        if os.getenv("AGENT_LIBOS_RUN_REAL_LLM_BENCHMARK") != "1":
            self.skipTest("real LLM benchmark smoke is opt-in")
        if not (os.getenv("OPENAI_API_KEY") and (os.getenv("OPENAI_LANGUAGE_MODEL") or os.getenv("OPENAI_MODEL"))):
            self.skipTest("real LLM environment is not configured")
        task = next(task for task in load_tasks(SUITE_ROOT) if task.id == "shell_allowed_version_001")
        with tempfile.TemporaryDirectory() as temp_dir:
            run = run_task(task, SUITE_ROOT, temp_dir, runner="agent_libos_full", llm_mode="real", max_quanta=1)

            self.assertGreaterEqual(run.result.metadata.get("llm_calls", 0), 1)


if __name__ == "__main__":
    unittest.main()

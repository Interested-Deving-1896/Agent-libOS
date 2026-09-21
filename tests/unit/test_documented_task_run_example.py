from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_libos import Runtime
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models import TaskRunStatus
from agent_libos.runtime.task_runs import TaskRunManager
from examples.task_runs.host_lifecycle import run_demo
from tests.support.network import offline_network


ROOT = Path(__file__).resolve().parents[2]


def test_documented_task_run_example_uses_real_completion_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline_network: None,
) -> None:
    commands: list[tuple[str, str]] = []

    def instrument(name: str) -> None:
        original = getattr(TaskRunManager, name)

        def checked(self: TaskRunManager, run_id: str, **kwargs: Any) -> Any:
            # Check the actual public CAS request at each mutation, not a
            # hard-coded revision: each quantum can advance it several times.
            assert kwargs["expected_revision"] == self.get(run_id).revision
            commands.append((name, kwargs["command_id"]))
            return original(self, run_id, **kwargs)

        monkeypatch.setattr(TaskRunManager, name, checked)

    for name in ("run_until_blocked", "pause", "resume"):
        instrument(name)

    database = tmp_path / "example.sqlite"
    report = run_demo(database)
    assert report["states"] == {
        "created": "queued",
        "wait_before_dispatch": "queued",
        "after_one_quantum": "running",
        "paused": "paused",
        "resumed": "running",
        "finished": "succeeded",
    }
    assert report["local_completions_after_wait"] == 0
    assert report["local_completions_after_resume"] == 1
    assert report["local_completions_total"] == 3
    assert report["satisfied_requirements"] == 1
    assert report["result"] == {"summary": "Local Skill catalog inspected."}
    assert [name for name, _ in commands] == [
        "run_until_blocked", "pause", "resume", "run_until_blocked",
    ]
    assert len({command_id for _, command_id in commands}) == len(commands)

    # Inspect the durable completion proof, rather than trusting the printed
    # status or fixture's claim that it read the catalog.
    config = replace(
        DEFAULT_CONFIG,
        task_runs=replace(DEFAULT_CONFIG.task_runs, plaintext_payloads_enabled=True),
    )
    runtime = Runtime.open(database, config=config)
    try:
        runs = runtime.task_runs.list().records
        assert len(runs) == 1
        run = runs[0]
        assert run.status is TaskRunStatus.SUCCEEDED
        assert run.payloads_purged is False
        ledger = runtime.task_runs.list_ledger(run.run_id, limit=100).records
        proofs = [
            item for item in ledger
            if item.label == "requirement satisfied by integrity-bound root exit"
        ]
        assert len(proofs) == 1
        receipt_ids = proofs[0].metadata["evidence_receipt_ids"]
        assert len(receipt_ids) == 1
        operation = runtime.store.get_operation(receipt_ids[0])
        assert operation is not None
        assert operation.name == "tool.discover_skills"
    finally:
        runtime.close()


def test_documented_task_run_example_runs_as_a_script() -> None:
    completed = subprocess.run(
        [sys.executable, "examples/task_runs/host_lifecycle.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    report = json.loads(completed.stdout)
    assert report["states"]["finished"] == "succeeded"
    assert report["local_completions_total"] == 3

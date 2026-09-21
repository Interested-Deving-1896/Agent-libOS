#!/usr/bin/env python3
"""Run an offline TaskRun lifecycle with the real completion review gate.

From the repository root: uv run python examples/task_runs/host_lifecycle.py
The scripted client implements complete_action without a provider or API key.
This demonstrates one Host session, not restart or crash recovery. The on-disk
temporary SQLite database exercises real persistence, then is removed on exit;
permanent retention keeps plaintext only while that example database exists.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_libos import Runtime, TaskRunRetention, TaskRunSpecV1, TaskRunStatus
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.llm.client import LLMCompletion


def _find_review(value: Any) -> dict[str, Any] | None:
    """Read the review supplied in the actual model-visible tool response."""
    if isinstance(value, str):
        try:
            return _find_review(json.loads(value))
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict):
        review = value.get("completion_review")
        if isinstance(review, dict) and isinstance(review.get("review_token"), str):
            return review
        value = list(value.values())
    if isinstance(value, list):
        for item in reversed(value):
            if (review := _find_review(item)) is not None:
                return review
    return None


class CatalogDemoClient:
    """Three local completions; the Runtime still validates and runs every tool."""

    def __init__(self) -> None:
        self.calls = 0

    def complete_action(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMCompletion:
        del tools
        self.calls += 1
        if self.calls == 1:
            name = "discover_skills"
            arguments: dict[str, Any] = {"text": "local Skill catalog", "limit": 5}
        else:
            name = "process_exit"
            arguments = {"payload": {"summary": "Local Skill catalog inspected."}}
            if self.calls == 3:
                review = _find_review(messages)
                if review is None or len(review["task_run"]["requirements"]) != 1:
                    raise RuntimeError("Expected one real completion-review requirement")
                arguments.update(
                    review_token=review["review_token"],
                    completion_evidence={
                        "goal_oid": review["goal"]["oid"],
                        "reviewed_message_ids": review["acknowledged_human_message_ids"],
                        "acceptance_checks": [
                            {
                                "requirement": "Inspect the local Skill catalog.",
                                "source_refs": review["completion_source_refs"],
                                "status": "completed",
                                "evidence_tool_calls": ["discover_skills"],
                                "evidence_summary": (
                                    "The successful catalog read verifies "
                                    "the requested inspection."
                                ),
                            },
                        ],
                        "final_verification": ["discover_skills"],
                    },
                )
            elif self.calls != 2:
                raise RuntimeError("The demo unexpectedly requested another completion")
        return LLMCompletion(
            content="",
            tool_calls=[
                {
                    "id": f"catalog-demo-{self.calls}",
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
            ],
        )


def run_demo(database: Path) -> dict[str, Any]:
    """Run against a fresh SQLite path and return a stable, printable report."""
    config = replace(
        DEFAULT_CONFIG,
        task_runs=replace(
            DEFAULT_CONFIG.task_runs, enabled=True, plaintext_payloads_enabled=True,
        ),
    )
    runtime = Runtime.open(database, config=config)
    client = CatalogDemoClient()
    runtime.llm.client = client
    try:
        run = runtime.task_runs.create(
            TaskRunSpecV1(
                goal="Inspect the local Skill catalog with discover_skills and finish.",
                display_title="Offline TaskRun lifecycle",
                image_id="coding-agent:v0",
                retention=TaskRunRetention.PERMANENT,
            ),
            client_request_id="catalog-demo:create",
        )
        states = {"created": run.status.value}
        observed = runtime.task_runs.wait(
            run.run_id, after_revision=run.revision, timeout=0,
        )
        states["wait_before_dispatch"] = observed.status.value
        calls_after_wait = client.calls

        run = runtime.task_runs.run_until_blocked(
            run.run_id, expected_revision=observed.revision,
            command_id="catalog-demo:inspect", max_quanta=1,
        )
        states["after_one_quantum"] = run.status.value
        run = runtime.task_runs.pause(
            run.run_id, expected_revision=run.revision,
            command_id="catalog-demo:pause",
        )
        states["paused"] = run.status.value

        # Refresh immediately before a new mutation; revisions are not counters
        # for Host commands and must never be guessed or incremented locally.
        current = runtime.task_runs.get(run.run_id)
        run = runtime.task_runs.resume(
            run.run_id, expected_revision=current.revision,
            command_id="catalog-demo:resume",
        )
        states["resumed"] = run.status.value
        calls_after_resume = client.calls  # Resume makes work eligible; it does not run it.

        # The first exit requests review; the next submits its real token and
        # evidence from the earlier tool call. No Host-forced process exit.
        run = runtime.task_runs.run_until_blocked(
            run.run_id, expected_revision=run.revision,
            command_id="catalog-demo:finish", max_quanta=2,
        )
        states["finished"] = run.status.value
        if run.status is not TaskRunStatus.SUCCEEDED or run.result_ref is None:
            raise RuntimeError(f"Demo did not finish: {run.status.value}, {run.blockers}")
        # This is a trusted Host Store read, not a model-facing memory grant.
        result = runtime.store.get_object(run.result_ref)
        if result is None:
            raise RuntimeError("The final result is unavailable")
        return {
            "states": states,
            "local_completions_after_wait": calls_after_wait,
            "local_completions_after_resume": calls_after_resume,
            "local_completions_total": client.calls,
            "satisfied_requirements": run.satisfied_requirement_count,
            "result": {"summary": result.payload["summary"]},
        }
    finally:
        runtime.close()


def main() -> None:
    with TemporaryDirectory(prefix="agent-libos-task-run-") as directory:
        print(json.dumps(run_demo(Path(directory) / "runtime.sqlite"), indent=2))


if __name__ == "__main__":
    main()

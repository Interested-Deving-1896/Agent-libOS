from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from agent_libos import Runtime
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import ProcessStatus, ResourceBudget


class TestResourceBudgets:
    def test_tool_call_budget_is_consumed_and_denies_next_tool_before_execution(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(
                image="base-agent:v0",
                goal="one tool",
                resource_budget=ResourceBudget(max_tool_calls=1),
            )
            runtime.tools.configure_process_tools(pid, ["get_working_directory"], assigned_by="test")

            first = runtime.tools.call(pid, "get_working_directory", {})
            second = runtime.tools.call(pid, "get_working_directory", {})

            assert first.ok, first.error
            assert not second.ok
            assert "max_tool_calls" in (second.error or "")
            assert runtime.process.get(pid).resource_usage.tool_calls == 1
        finally:
            runtime.close()

    def test_llm_token_overage_kills_process_before_dispatching_tool(self) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.llm.client = UsageClient(total_tokens=11)
            pid = runtime.process.spawn(
                image="base-agent:v0",
                goal="exit but over budget",
                resource_budget=ResourceBudget(max_llm_total_tokens=10),
            )

            result = runtime.run_next_process_once()
            process = runtime.process.get(pid)

            assert not result["ok"]
            assert result["resource_limit_exceeded"]
            assert process.status == ProcessStatus.KILLED
            assert process.resource_usage.llm_total_tokens == 11
            assert not any(record.action == "process.exit" and record.actor == pid for record in runtime.audit.trace())
        finally:
            runtime.close()

    def test_llm_missing_usage_with_token_budget_kills_process(self) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.llm.client = UsageClient(total_tokens=None)
            pid = runtime.process.spawn(
                image="base-agent:v0",
                goal="missing usage",
                resource_budget=ResourceBudget(max_llm_total_tokens=10),
            )

            result = runtime.run_next_process_once()
            process = runtime.process.get(pid)

            assert not result["ok"]
            assert result["resource_limit_exceeded"]
            assert process.status == ProcessStatus.KILLED
            assert process.resource_usage.llm_calls == 1
            assert process.resource_usage.llm_total_tokens == 0
        finally:
            runtime.close()

    def test_child_llm_usage_counts_against_parent_budget(self) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.llm.client = UsageClient(total_tokens=7)
            parent = runtime.process.spawn(
                image="base-agent:v0",
                goal="parent",
                resource_budget=ResourceBudget(max_llm_total_tokens=20),
            )
            child = runtime.process.spawn_child(
                parent,
                goal="child",
                resource_budget=ResourceBudget(max_llm_total_tokens=10),
            )

            runtime.run_process_once(child)

            assert runtime.process.get(child).resource_usage.llm_total_tokens == 7
            assert runtime.process.get(parent).resource_usage.llm_total_tokens == 7
        finally:
            runtime.close()

    def test_failed_llm_provider_call_consumes_call_budget_and_persists_sanitized_error_record(self) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.llm.client = FailingClient()
            pid = runtime.process.spawn(
                image="base-agent:v0",
                goal="provider fails with secret-token",
                resource_budget=ResourceBudget(max_llm_calls=1),
            )

            result = runtime.run_next_process_once()
            process = runtime.process.get(pid)
            calls = runtime.store.list_llm_calls(pid)

            assert not result["ok"]
            assert process.status == ProcessStatus.FAILED
            assert process.resource_usage.llm_calls == 1
            assert len(calls) == 1
            assert calls[0].status == "error"
            assert calls[0].messages["sha256"]
            assert "secret-token" not in json.dumps(calls[0].__dict__, sort_keys=True)
        finally:
            runtime.close()


class UsageClient:
    def __init__(self, total_tokens: int | None) -> None:
        self.total_tokens = total_tokens

    def complete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        usage = {} if self.total_tokens is None else {"prompt_tokens": 5, "completion_tokens": self.total_tokens - 5, "total_tokens": self.total_tokens}
        return LLMCompletion(
            content="",
            tool_calls=[
                {
                    "id": "tool_1",
                    "name": "process_exit",
                    "arguments": json.dumps({"payload": {"done": True}}),
                }
            ],
            raw=SimpleNamespace(id="raw"),
            api="chat",
            response_id="resp_1",
            request_id="req_1",
            model="test-model",
            usage=usage,
        )


class FailingClient:
    def complete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        raise RuntimeError("provider unavailable")

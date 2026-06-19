from __future__ import annotations

import tempfile

import pytest

from agent_libos import Runtime
from agent_libos.models import ResourceBudget, ResourceUsage
from agent_libos.models.exceptions import ResourceLimitExceeded


class TestResourceManager:
    def test_hierarchical_charge_updates_child_and_parent_usage(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(
                image="base-agent:v0",
                goal="parent",
                resource_budget=ResourceBudget(max_tool_calls=4, max_llm_total_tokens=100),
            )
            child = runtime.process.spawn_child(
                parent,
                goal="child",
                resource_budget=ResourceBudget(max_tool_calls=2, max_llm_total_tokens=50),
            )

            runtime.resources.charge(
                child,
                ResourceUsage(tool_calls=1, llm_total_tokens=7),
                source="test",
            )

            assert runtime.process.get(child).resource_usage.tool_calls == 1
            assert runtime.process.get(parent).resource_usage.tool_calls == 1
            assert runtime.process.get(child).resource_usage.llm_total_tokens == 7
            assert runtime.process.get(parent).resource_usage.llm_total_tokens == 7
        finally:
            runtime.close()

    def test_preflight_fails_without_consuming_budget(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(
                image="base-agent:v0",
                goal="budget",
                resource_budget=ResourceBudget(max_tool_calls=1),
            )
            runtime.resources.charge(pid, ResourceUsage(tool_calls=1), source="test")

            with pytest.raises(ResourceLimitExceeded):
                runtime.resources.preflight(pid, ResourceUsage(tool_calls=1), source="test")

            assert runtime.process.get(pid).resource_usage.tool_calls == 1
        finally:
            runtime.close()

    def test_resource_usage_is_persisted_with_process_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = f"{temp_dir}/runtime.sqlite"
            runtime = Runtime.open(db)
            try:
                pid = runtime.process.spawn(image="base-agent:v0", goal="persist")
                runtime.resources.charge(pid, ResourceUsage(llm_calls=1, llm_total_tokens=9), source="test")
            finally:
                runtime.close()

            reopened = Runtime.open(db)
            try:
                process = reopened.process.get(pid)
                assert process.resource_usage.llm_calls == 1
                assert process.resource_usage.llm_total_tokens == 9
            finally:
                reopened.close()

    def test_child_budget_cannot_exceed_parent_remaining_budget(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(
                image="base-agent:v0",
                goal="parent",
                resource_budget=ResourceBudget(max_tool_calls=2),
            )
            runtime.resources.charge(parent, ResourceUsage(tool_calls=1), source="test")

            with pytest.raises(ResourceLimitExceeded):
                runtime.process.spawn_child(
                    parent,
                    goal="oversized child",
                    resource_budget=ResourceBudget(max_tool_calls=2),
                )
        finally:
            runtime.close()

    def test_jsonrpc_remaining_budget_counts_request_and_response_together(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(
                image="base-agent:v0",
                goal="jsonrpc budget",
                resource_budget=ResourceBudget(max_jsonrpc_bytes=100),
            )
            runtime.resources.charge(
                pid,
                ResourceUsage(jsonrpc_request_bytes=40, jsonrpc_response_bytes=40),
                source="test",
            )

            assert runtime.resources.remaining_budget(pid).max_jsonrpc_bytes == 20
            with pytest.raises(ResourceLimitExceeded):
                runtime.process.spawn_child(
                    pid,
                    goal="oversized jsonrpc child",
                    resource_budget=ResourceBudget(max_jsonrpc_bytes=30),
                )
        finally:
            runtime.close()

    def test_child_process_creation_is_hierarchical_usage(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(
                image="base-agent:v0",
                goal="parent",
                resource_budget=ResourceBudget(max_child_processes=1),
            )
            child = runtime.process.spawn_child(parent, goal="child")

            assert runtime.process.get(parent).resource_usage.child_processes == 1
            with pytest.raises(ResourceLimitExceeded):
                runtime.process.spawn_child(child, goal="grandchild")
            assert runtime.process.get(parent).resource_usage.child_processes == 1
        finally:
            runtime.close()

    def test_parent_budget_exceedance_kills_parent_subtree(self) -> None:
        runtime = Runtime.open("local")
        try:
            parent = runtime.process.spawn(
                image="base-agent:v0",
                goal="parent",
                resource_budget=ResourceBudget(max_llm_total_tokens=10),
            )
            child = runtime.process.spawn_child(
                parent,
                goal="child",
                resource_budget=ResourceBudget(max_llm_total_tokens=None),
            )

            with pytest.raises(ResourceLimitExceeded):
                runtime.resources.charge(
                    child,
                    ResourceUsage(llm_total_tokens=11),
                    source="test",
                    allow_overage=True,
                    kill_on_exceed=True,
                )

            assert runtime.process.get(parent).status.value == "killed"
            assert runtime.process.get(child).status.value == "killed"
        finally:
            runtime.close()

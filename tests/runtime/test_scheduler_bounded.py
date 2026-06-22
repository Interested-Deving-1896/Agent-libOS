from __future__ import annotations

import asyncio

from agent_libos import Runtime
from agent_libos.models import ProcessStatus, ResourceBudget


class TestBoundedScheduler:
    def test_bounded_run_spends_unblock_quantum_for_waited_child(self) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.scheduler.poll_interval_s = 0.001
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            state: dict[str, str] = {}

            async def quantum(pid: str):
                if pid == parent:
                    child = state.get("child")
                    if child is None:
                        child = runtime.process.spawn_child(
                            parent,
                            goal="child",
                            resource_budget=ResourceBudget(max_tool_calls=0, max_child_processes=None),
                        )
                        state["child"] = child
                    process = runtime.process.get(parent)
                    process.status = ProcessStatus.WAITING_EVENT
                    process.status_message = f"waiting for {child}"
                    runtime.store.update_process(process)
                    while runtime.process.get(child).status not in runtime.process.TERMINAL_STATUSES:
                        await asyncio.sleep(runtime.scheduler.poll_interval_s)
                    return {"pid": pid, "done": "parent"}
                runtime.process.exit(pid)
                return {"pid": pid, "done": "child"}

            results = asyncio.run(asyncio.wait_for(runtime.scheduler.arun_until_idle(quantum, max_quanta=1), timeout=1.0))

            assert {item["done"] for item in results if isinstance(item, dict) and "done" in item} == {
                "parent",
                "child",
            }
        finally:
            runtime.close()

    def test_bounded_run_cancels_unresolved_blocked_quantum(self) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.scheduler.poll_interval_s = 0.001
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            state: dict[str, str] = {}

            async def quantum(pid: str):
                if pid == parent:
                    child = state.get("child")
                    if child is None:
                        child = runtime.process.spawn_child(
                            parent,
                            goal="child",
                            resource_budget=ResourceBudget(max_tool_calls=0, max_child_processes=None),
                        )
                        state["child"] = child
                    process = runtime.process.get(parent)
                    process.status = ProcessStatus.WAITING_EVENT
                    process.status_message = f"waiting for {child}"
                    runtime.store.update_process(process)
                    while runtime.process.get(child).status not in runtime.process.TERMINAL_STATUSES:
                        await asyncio.sleep(runtime.scheduler.poll_interval_s)
                    return {"pid": pid, "done": "parent"}
                await asyncio.sleep(60)
                return {"pid": pid, "done": "child"}

            asyncio.run(asyncio.wait_for(runtime.scheduler.arun_until_idle(quantum, max_quanta=1), timeout=1.0))

            records = [record for record in runtime.audit.trace() if record.action == "scheduler.process_task_cancelled"]
            assert records
        finally:
            runtime.close()

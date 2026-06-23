from __future__ import annotations

import asyncio
import threading
import time

from agent_libos import Runtime
from agent_libos.config import AgentLibOSConfig, SchedulerDefaults
from agent_libos.models import ProcessStatus, ResourceBudget


def _parallel_config(max_workers: int = 4) -> AgentLibOSConfig:
    return AgentLibOSConfig(
        scheduler=SchedulerDefaults(
            max_workers=max_workers,
            poll_interval_s=0.001,
            drain_window_s=0.3,
            shutdown_join_timeout_s=0.5,
        )
    )


class TestParallelScheduler:
    def test_blocking_quantum_does_not_block_other_process(self) -> None:
        runtime = Runtime.open("local", config=_parallel_config(max_workers=2))
        try:
            slow = runtime.process.spawn(image="base-agent:v0", goal="slow")
            fast = runtime.process.spawn(image="base-agent:v0", goal="fast")
            slow_started = threading.Event()
            marks: dict[str, float] = {}
            lock = threading.Lock()

            def mark(name: str) -> None:
                with lock:
                    marks[name] = time.perf_counter()

            def quantum(pid: str) -> dict[str, str]:
                if pid == slow:
                    slow_started.set()
                    time.sleep(0.15)
                    runtime.process.pause(pid, "slow quantum complete")
                    mark("slow_done")
                    return {"pid": pid, "kind": "slow"}
                assert slow_started.wait(timeout=0.5)
                runtime.process.exit(pid)
                mark("fast_done")
                return {"pid": pid, "kind": "fast"}

            results = runtime.scheduler.run_until_idle(quantum, max_quanta=2)

            assert {result["kind"] for result in results if isinstance(result, dict)} == {"slow", "fast"}
            assert marks["fast_done"] < marks["slow_done"]
        finally:
            runtime.close()

    def test_same_process_quantum_is_not_reentered(self) -> None:
        runtime = Runtime.open("local", config=_parallel_config(max_workers=4))
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="single process")
            active = 0
            max_active = 0
            calls = 0
            lock = threading.Lock()

            def quantum(selected_pid: str) -> dict[str, int | str]:
                nonlocal active, max_active, calls
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    time.sleep(0.02)
                    with lock:
                        calls += 1
                        call_index = calls
                    if call_index >= 2:
                        runtime.process.pause(selected_pid, "done")
                    return {"pid": selected_pid, "call": call_index}
                finally:
                    with lock:
                        active -= 1

            results = runtime.scheduler.run_until_idle(quantum, max_quanta=2)

            assert [result["call"] for result in results if isinstance(result, dict)] == [1, 2]
            assert max_active == 1
        finally:
            runtime.close()

    def test_parallel_scheduler_respects_global_quantum_budget(self) -> None:
        runtime = Runtime.open("local", config=_parallel_config(max_workers=4))
        try:
            for index in range(4):
                runtime.process.spawn(image="base-agent:v0", goal=f"process {index}")

            def quantum(pid: str) -> dict[str, str]:
                runtime.process.pause(pid, "budget consumed")
                return {"pid": pid}

            results = runtime.scheduler.run_until_idle(quantum, max_quanta=2)
            run_records = [record for record in runtime.audit.trace() if record.action == "scheduler.run_quantum"]

            assert len([result for result in results if isinstance(result, dict)]) == 2
            assert len(run_records) == 2
        finally:
            runtime.close()

    def test_waiting_parent_does_not_starve_child_when_worker_pool_is_full(self) -> None:
        runtime = Runtime.open("local", config=_parallel_config(max_workers=1))
        try:
            parent = runtime.process.spawn(image="base-agent:v0", goal="parent")
            state: dict[str, str] = {}

            async def quantum(pid: str) -> dict[str, str]:
                if pid == parent:
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

            results = asyncio.run(runtime.scheduler.arun_until_idle(quantum, max_quanta=1))

            assert {item["done"] for item in results if isinstance(item, dict) and "done" in item} == {
                "parent",
                "child",
            }
        finally:
            runtime.close()

    def test_concurrent_top_level_runs_do_not_reenter_same_process(self) -> None:
        runtime = Runtime.open("local", config=_parallel_config(max_workers=4))
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="single process")
            active = 0
            max_active = 0
            lock = threading.Lock()
            start = threading.Barrier(2)
            errors: list[BaseException] = []

            def quantum(selected_pid: str) -> dict[str, str]:
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    time.sleep(0.05)
                    runtime.process.pause(selected_pid, "done")
                    return {"pid": selected_pid}
                finally:
                    with lock:
                        active -= 1

            def runner() -> None:
                try:
                    start.wait(timeout=1.0)
                    runtime.scheduler.run_until_idle(quantum, max_quanta=1)
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=runner) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=1.0)

            assert errors == []
            assert max_active == 1
        finally:
            runtime.close()

    def test_shutdown_leaves_store_open_when_sync_quantum_cannot_stop(self) -> None:
        config = AgentLibOSConfig(
            scheduler=SchedulerDefaults(
                max_workers=1,
                poll_interval_s=0.001,
                drain_window_s=0.01,
                shutdown_join_timeout_s=0.01,
            )
        )
        runtime = Runtime.open("local", config=config)
        pid = runtime.process.spawn(image="base-agent:v0", goal="slow sync")
        try:
            def quantum(selected_pid: str) -> dict[str, str]:
                time.sleep(0.1)
                runtime.process.exit(selected_pid)
                return {"pid": selected_pid}

            assert runtime.scheduler.run_until_idle(quantum, max_quanta=1) == []
            result = runtime.shutdown(actor="test", reason="detached quantum")

            assert result["ok"] is False
            assert result["scheduler_stopped"] is False
            assert runtime.store.get_process(pid) is not None
            time.sleep(0.15)
            assert runtime.shutdown(actor="test", reason="detached quantum complete")["ok"] is True
        finally:
            runtime.close()

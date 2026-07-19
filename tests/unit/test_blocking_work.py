from __future__ import annotations

import asyncio
import threading

import pytest

from agent_libos.ports.blocking_work import run_blocking_once
from agent_libos.runtime.blocking_work import BlockingWorkSupervisor


def test_run_blocking_once_propagates_worker_cancelled_error_without_spinning() -> None:
    def worker() -> None:
        raise asyncio.CancelledError("worker cancelled itself")

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError, match="worker cancelled itself"):
            await asyncio.wait_for(run_blocking_once(worker), timeout=1)

    asyncio.run(exercise())


def test_runtime_supervisor_propagates_worker_cancelled_error_without_spinning() -> None:
    async def exercise() -> None:
        supervisor = BlockingWorkSupervisor(max_workers=1, shutdown_timeout_s=1)

        def worker() -> None:
            raise asyncio.CancelledError("supervised worker cancelled itself")

        try:
            with pytest.raises(
                asyncio.CancelledError,
                match="supervised worker cancelled itself",
            ):
                await asyncio.wait_for(supervisor.run(worker), timeout=1)
        finally:
            assert await supervisor.ashutdown()

    asyncio.run(exercise())


def test_run_blocking_once_aggregates_caller_cancellation_and_worker_failure() -> None:
    async def exercise() -> None:
        entered = threading.Event()
        release = threading.Event()

        def worker() -> None:
            entered.set()
            assert release.wait(timeout=1)
            raise RuntimeError("worker failed after cancellation")

        task = asyncio.create_task(run_blocking_once(worker))
        for _ in range(1000):
            if entered.is_set():
                break
            await asyncio.sleep(0)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(BaseExceptionGroup) as caught:
            await task
        assert caught.value.subgroup(asyncio.CancelledError) is not None
        assert caught.value.subgroup(RuntimeError) is not None

    asyncio.run(exercise())

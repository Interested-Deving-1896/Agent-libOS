from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future, ThreadPoolExecutor, wait
from functools import partial
from typing import Any, Callable, TypeVar

from agent_libos.ports.blocking_work import run_blocking_once


T = TypeVar("T")


def _settled_worker_error(future: Future[Any]) -> BaseException | None:
    if not future.done():
        return None
    try:
        future.result()
    except BaseException as exc:
        return exc
    return None


class BlockingWorkSupervisor:
    """Runtime-owned execution and drain boundary for blocking Host work."""

    def __init__(self, *, max_workers: int, shutdown_timeout_s: float) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="agent-libos-blocking",
        )
        self._shutdown_timeout_s = max(0.0, float(shutdown_timeout_s))
        self._lock = threading.RLock()
        self._futures: set[Future[Any]] = set()
        self._closing = False
        self._closed = False

    async def run(
        self,
        function: Callable[..., T],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> T:
        context = contextvars.copy_context()
        call = partial(function, *args, **kwargs)
        with self._lock:
            if self._closing or self._closed:
                raise RuntimeError("runtime blocking-work supervisor is shutting down")
            future = self._executor.submit(context.run, call)
            self._futures.add(future)
            future.add_done_callback(self._forget)

        wrapped = asyncio.wrap_future(future)
        cancelled = False
        caller_task = asyncio.current_task()
        initial_cancellations = (
            caller_task.cancelling() if caller_task is not None else 0
        )
        while True:
            try:
                result = await asyncio.shield(wrapped)
            except asyncio.CancelledError as exc:
                worker_error = _settled_worker_error(future)
                if worker_error is not None:
                    selected_error: BaseException = (
                        exc
                        if isinstance(worker_error, FutureCancelledError)
                        else worker_error
                    )
                    caller_cancelled_now = bool(
                        caller_task is not None
                        and caller_task.cancelling() > initial_cancellations
                    )
                    if cancelled or caller_cancelled_now:
                        raise BaseExceptionGroup(
                            "runtime blocking work was cancelled while its worker failed",
                            [asyncio.CancelledError(), selected_error],
                        ) from None
                    raise selected_error
                cancelled = True
                continue
            except BaseException as exc:
                if cancelled:
                    raise BaseExceptionGroup(
                        "runtime blocking work was cancelled while its worker failed",
                        [asyncio.CancelledError(), exc],
                    ) from None
                raise
            if cancelled:
                raise asyncio.CancelledError()
            return result

    def _forget(self, future: Future[Any]) -> None:
        with self._lock:
            self._futures.discard(future)

    def active_count(self) -> int:
        with self._lock:
            return sum(not future.done() for future in self._futures)

    def shutdown(self) -> bool:
        with self._lock:
            if self._closed:
                return True
            self._closing = True
            pending = [future for future in self._futures if not future.done()]
        if pending:
            wait(pending, timeout=self._shutdown_timeout_s)
        with self._lock:
            if any(not future.done() for future in self._futures):
                return False
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._closed = True
            return True

    async def ashutdown(self) -> bool:
        # This supervisor cannot submit its own shutdown to the executor that it
        # is closing. A one-call owned executor is drained by the await itself.
        return await run_blocking_once(self.shutdown)


__all__ = ["BlockingWorkSupervisor"]

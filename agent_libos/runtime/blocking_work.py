from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from functools import partial
from typing import Any, Callable, TypeVar


T = TypeVar("T")


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
        while True:
            try:
                result = await asyncio.shield(wrapped)
            except asyncio.CancelledError:
                cancelled = True
                continue
            except BaseException:
                if cancelled:
                    raise asyncio.CancelledError() from None
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
        return await asyncio.get_running_loop().run_in_executor(None, self.shutdown)


__all__ = ["BlockingWorkSupervisor"]

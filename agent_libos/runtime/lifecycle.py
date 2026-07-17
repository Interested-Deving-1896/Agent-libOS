from __future__ import annotations

import asyncio
import inspect
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator

from agent_libos.models import EventType
from agent_libos.utils.ids import new_id


class _LifecycleState(str, Enum):
    NEW = "new"
    RECOVERING = "recovering"
    STARTING = "starting"
    OPEN = "open"
    STOPPING = "stopping"
    CLOSE_FAILED = "close_failed"
    CLOSED = "closed"


@dataclass(slots=True)
class _ShutdownAttempt:
    owner_thread_id: int
    owner_task: asyncio.Task[Any] | None
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: BaseException | None = None


@dataclass(slots=True)
class _FinalizerEntry:
    handle: str
    callback: Any
    completed: bool = False


class RuntimeLifecycle:
    """Single-source Runtime lifecycle, admission gate, and teardown engine."""

    def __init__(
        self,
        *,
        store: Any,
        audit: Any,
        events: Any,
        substrate: Any,
        admission_drain_timeout_s: float = 2.0,
    ) -> None:
        self._store = store
        self._audit = audit
        self._events = events
        self._substrate = substrate
        self._scheduler: Any | None = None
        self._object_tasks: Any | None = None
        self._modules: Any | None = None
        self._llms: Any | None = None
        self._blocking_work: Any | None = None
        self._components_bound = False
        self._finalizers: list[_FinalizerEntry] = []
        self._lock = threading.RLock()
        self._admission_condition = threading.Condition(self._lock)
        self._state = _LifecycleState.NEW
        self._shutdown_reason: str | None = None
        self._active_attempt: _ShutdownAttempt | None = None
        self._active_leases = 0
        self._admission_drain_timeout_s = max(0.0, float(admission_drain_timeout_s))
        self._recovery_token = object()
        self._startup_token = object()
        self._internal_admission: ContextVar[object | None] = ContextVar(
            f"agent_libos_internal_admission_{id(self)}",
            default=None,
        )
        self._shutdown_attempt_context: ContextVar[_ShutdownAttempt | None] = ContextVar(
            f"agent_libos_runtime_shutdown_attempt_{id(self)}",
            default=None,
        )

    @property
    def state(self) -> str:
        with self._lock:
            return self._state.value

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._state is _LifecycleState.CLOSED

    @property
    def shutdown_reason(self) -> str | None:
        with self._lock:
            return self._shutdown_reason

    def begin_recovery(self) -> None:
        self._transition(_LifecycleState.NEW, _LifecycleState.RECOVERING)

    def begin_starting(self) -> None:
        self._transition(_LifecycleState.RECOVERING, _LifecycleState.STARTING)

    def mark_open(self) -> None:
        self._transition(_LifecycleState.STARTING, _LifecycleState.OPEN)

    def _transition(self, expected: _LifecycleState, selected: _LifecycleState) -> None:
        with self._lock:
            if self._state is not expected:
                raise RuntimeError(
                    f"invalid runtime lifecycle transition: {self._state.value} -> {selected.value}"
                )
            self._state = selected

    @contextmanager
    def recovery_lease(self) -> Iterator[None]:
        with self._internal_lease(self._recovery_token, _LifecycleState.RECOVERING):
            yield

    @contextmanager
    def startup_lease(self) -> Iterator[None]:
        with self._internal_lease(self._startup_token, _LifecycleState.STARTING):
            yield

    @contextmanager
    def _internal_lease(self, token: object, expected: _LifecycleState) -> Iterator[None]:
        with self._lock:
            if self._state is not expected:
                raise RuntimeError(
                    f"internal lifecycle lease requires {expected.value}, got {self._state.value}"
                )
        reset = self._internal_admission.set(token)
        try:
            yield
        finally:
            self._internal_admission.reset(reset)

    @contextmanager
    def admit(self, *, read_only: bool = False) -> Iterator[None]:
        """Acquire an operation lease before any operation/effect mutation."""

        with self._admission_condition:
            internal = self._internal_admission.get()
            allowed_internal = (
                (self._state is _LifecycleState.RECOVERING and internal is self._recovery_token)
                or (self._state is _LifecycleState.STARTING and internal is self._startup_token)
            )
            allowed = (
                self._state is _LifecycleState.OPEN
                or allowed_internal
                or (read_only and self._state is not _LifecycleState.CLOSED)
            )
            if not allowed:
                raise RuntimeError(
                    f"runtime is not accepting operations: state={self._state.value}"
                )
            self._active_leases += 1
        try:
            yield
        finally:
            with self._admission_condition:
                self._active_leases -= 1
                if self._active_leases == 0:
                    self._admission_condition.notify_all()

    def bind_components(
        self,
        *,
        scheduler: Any,
        object_tasks: Any,
        modules: Any,
        llms: Any,
        blocking_work: Any | None = None,
    ) -> None:
        with self._lock:
            if self._components_bound:
                raise RuntimeError("runtime lifecycle components are already bound")
            if self._active_attempt is not None or self._state in {
                _LifecycleState.STOPPING,
                _LifecycleState.CLOSE_FAILED,
                _LifecycleState.CLOSED,
            }:
                raise RuntimeError("cannot bind runtime components after shutdown has started")
            self._scheduler = scheduler
            self._object_tasks = object_tasks
            self._modules = modules
            self._llms = llms
            self._blocking_work = blocking_work
            self._components_bound = True

    def finalizers_snapshot(self) -> tuple[Any, ...]:
        with self._lock:
            return tuple(entry.callback for entry in self._finalizers)

    def bind_finalizer(self, finalizer: Any) -> None:
        with self._lock:
            if self._active_attempt is not None or self._state in {
                _LifecycleState.STOPPING,
                _LifecycleState.CLOSE_FAILED,
                _LifecycleState.CLOSED,
            }:
                raise RuntimeError("cannot bind a shutdown finalizer after shutdown has started")
            self._finalizers.append(
                _FinalizerEntry(handle=new_id("finalizer"), callback=finalizer)
            )

    def unbind_finalizer(self, finalizer: Any) -> bool:
        with self._lock:
            for index in range(len(self._finalizers) - 1, -1, -1):
                if self._finalizers[index].callback is finalizer:
                    del self._finalizers[index]
                    return True
        return False

    def shutdown(
        self,
        *,
        actor: str = "runtime",
        reason: str = "runtime.shutdown",
    ) -> dict[str, Any]:
        # Coroutine finalizers cannot be synchronously driven by the thread
        # that already owns an event loop. Refuse before closing admission.
        if self._running_loop() is not None and any(
            not entry.completed and inspect.iscoroutinefunction(entry.callback)
            for entry in self._finalizers
        ):
            raise RuntimeError(
                "runtime has async shutdown finalizers; use await runtime.ashutdown()"
            )
        caller_task = self._current_task()
        attempt, is_leader, early = self._start_attempt(caller_task=caller_task)
        if early is not None:
            return early
        assert attempt is not None
        if not is_leader:
            self._reject_reentrant_wait(attempt, caller_task=caller_task, async_wait=False)
            attempt.done.wait()
            return self._attempt_result(attempt)
        context_token = self._shutdown_attempt_context.set(attempt)
        try:
            result = self._shutdown_sync(actor=actor, reason=reason)
        except BaseException as exc:
            with self._lock:
                self._state = _LifecycleState.CLOSE_FAILED
            self._complete_attempt(attempt, error=exc)
            raise
        else:
            self._complete_attempt(attempt, result=result)
            return result
        finally:
            self._shutdown_attempt_context.reset(context_token)

    async def ashutdown(
        self,
        *,
        actor: str = "runtime",
        reason: str = "runtime.shutdown",
    ) -> dict[str, Any]:
        caller_task = self._current_task()
        attempt, is_leader, early = self._start_attempt(caller_task=caller_task)
        if early is not None:
            return early
        assert attempt is not None
        if not is_leader:
            self._reject_reentrant_wait(attempt, caller_task=caller_task, async_wait=True)
            await asyncio.to_thread(attempt.done.wait)
            return self._attempt_result(attempt)
        context_token = self._shutdown_attempt_context.set(attempt)
        try:
            result = await self._shutdown_async(actor=actor, reason=reason)
        except BaseException as exc:
            with self._lock:
                self._state = _LifecycleState.CLOSE_FAILED
            self._complete_attempt(attempt, error=exc)
            raise
        else:
            self._complete_attempt(attempt, result=result)
            return result
        finally:
            self._shutdown_attempt_context.reset(context_token)

    def _shutdown_sync(self, *, actor: str, reason: str) -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        if not self._drain_admission():
            for name, component in (("scheduler", self._scheduler), ("object_tasks", self._object_tasks)):
                if not self._stop_sync_component(name, component, errors):
                    return self._failed(reason, name, errors)
            return self._failed(reason, "admission", errors)
        if not self._record_shutdown(actor=actor, reason=reason, errors=errors):
            return self._failed(reason, "shutdown_evidence", errors)
        for name, component in (("scheduler", self._scheduler), ("object_tasks", self._object_tasks)):
            if not self._stop_sync_component(name, component, errors):
                return self._failed(reason, name, errors)
        failed_finalizer = self._run_sync_finalizers(errors)
        if failed_finalizer is not None:
            return self._failed(reason, failed_finalizer, errors)
        for name, component in (
            ("modules", self._modules),
            ("llms", self._llms),
            ("blocking_work", self._blocking_work),
            ("substrate", self._substrate),
        ):
            if not self._stop_sync_component(name, component, errors):
                return self._failed(reason, name, errors)
        return self._close_store(reason, errors)

    async def _shutdown_async(self, *, actor: str, reason: str) -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        if not await asyncio.to_thread(self._drain_admission):
            for name, component in (("scheduler", self._scheduler), ("object_tasks", self._object_tasks)):
                if not await self._stop_async_component(name, component, errors):
                    return self._failed(reason, name, errors)
            return self._failed(reason, "admission", errors)
        if not self._record_shutdown(actor=actor, reason=reason, errors=errors):
            return self._failed(reason, "shutdown_evidence", errors)
        for name, component in (("scheduler", self._scheduler), ("object_tasks", self._object_tasks)):
            if not await self._stop_async_component(name, component, errors):
                return self._failed(reason, name, errors)
        failed_finalizer = await self._run_async_finalizers(errors)
        if failed_finalizer is not None:
            return self._failed(reason, failed_finalizer, errors)
        for name, component in (
            ("modules", self._modules),
            ("llms", self._llms),
            ("blocking_work", self._blocking_work),
            ("substrate", self._substrate),
        ):
            if not await self._stop_async_component(name, component, errors):
                return self._failed(reason, name, errors)
        return self._close_store(reason, errors)

    def cleanup_failed_assembly(self) -> list[dict[str, str]]:
        """Run the same teardown order without closing the diagnostic store."""

        errors: list[dict[str, str]] = []
        with self._lock:
            if self._state is not _LifecycleState.CLOSED:
                self._state = _LifecycleState.STOPPING
        for name, component in (("scheduler", self._scheduler), ("object_tasks", self._object_tasks)):
            self._stop_sync_component(name, component, errors)
        # Failed assembly finalizers are run exactly once and then unbound so
        # later rollback paths cannot replay them.
        for entry in list(self._finalizers):
            if not entry.completed:
                try:
                    result = entry.callback()
                    if inspect.isawaitable(result):
                        result = asyncio.run(result)
                    if result is False:
                        errors.append({"component": entry.handle, "error_type": "FinalizerDeferred", "error": "returned false"})
                except Exception as exc:
                    self._record_error(errors, entry.handle, exc)
                finally:
                    entry.completed = True
            self.unbind_finalizer(entry.callback)
        for name, component in (
            ("modules", self._modules),
            ("llms", self._llms),
            ("blocking_work", self._blocking_work),
            ("substrate", self._substrate),
        ):
            self._stop_sync_component(name, component, errors)
        with self._lock:
            if self._state is not _LifecycleState.CLOSED:
                self._state = _LifecycleState.CLOSE_FAILED
        return errors

    def _start_attempt(
        self,
        *,
        caller_task: asyncio.Task[Any] | None,
    ) -> tuple[_ShutdownAttempt | None, bool, dict[str, Any] | None]:
        with self._lock:
            if self._state is _LifecycleState.CLOSED:
                return None, False, {
                    "ok": True,
                    "already_shutdown": True,
                    "reason": self._shutdown_reason,
                }
            if self._active_attempt is not None:
                return self._active_attempt, False, None
            attempt = _ShutdownAttempt(
                owner_thread_id=threading.get_ident(),
                owner_task=caller_task,
            )
            self._active_attempt = attempt
            self._state = _LifecycleState.STOPPING
            self._shutdown_reason = self._shutdown_reason or "runtime.shutdown"
            return attempt, True, None

    def _drain_admission(self) -> bool:
        deadline = time.monotonic() + self._admission_drain_timeout_s
        with self._admission_condition:
            while self._active_leases:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._admission_condition.wait(timeout=remaining)
            return True

    def _record_shutdown(
        self,
        *,
        actor: str,
        reason: str,
        errors: list[dict[str, str]],
    ) -> bool:
        self._shutdown_reason = reason
        try:
            with self._store.transaction():
                self._audit.record(
                    actor=actor,
                    action="runtime.shutdown",
                    target="runtime",
                    decision={"reason": reason},
                )
                self._events.emit(
                    EventType.RUNTIME_SHUTDOWN,
                    source=actor,
                    target="runtime",
                    payload={"reason": reason},
                )
        except Exception as exc:
            self._record_error(errors, "shutdown_evidence", exc)
            return False
        return True

    def _run_sync_finalizers(self, errors: list[dict[str, str]]) -> str | None:
        for entry in self._finalizers:
            if entry.completed:
                continue
            try:
                result = entry.callback()
                if inspect.isawaitable(result):
                    if self._running_loop() is not None:
                        close = getattr(result, "close", None)
                        if callable(close):
                            close()
                        raise RuntimeError(
                            "async shutdown finalizer requires await runtime.ashutdown()"
                        )
                    result = asyncio.run(result)
                if result is False:
                    return entry.handle
                entry.completed = True
            except Exception as exc:
                self._record_error(errors, entry.handle, exc)
                return entry.handle
        return None

    async def _run_async_finalizers(self, errors: list[dict[str, str]]) -> str | None:
        for entry in self._finalizers:
            if entry.completed:
                continue
            try:
                result = entry.callback()
                if inspect.isawaitable(result):
                    result = await result
                if result is False:
                    return entry.handle
                entry.completed = True
            except Exception as exc:
                self._record_error(errors, entry.handle, exc)
                return entry.handle
        return None

    def _close_store(self, reason: str, errors: list[dict[str, str]]) -> dict[str, Any]:
        try:
            self._store.close()
        except Exception as exc:
            self._record_error(errors, "store", exc)
            return self._failed(reason, "store", errors)
        with self._lock:
            self._state = _LifecycleState.CLOSED
        return {"ok": True, "already_shutdown": False, "reason": reason}

    def _failed(
        self,
        reason: str,
        component: str,
        errors: list[dict[str, str]],
    ) -> dict[str, Any]:
        with self._lock:
            self._state = _LifecycleState.CLOSE_FAILED
        result: dict[str, Any] = {
            "ok": False,
            "already_shutdown": False,
            "reason": reason,
            f"{component}_stopped": False,
        }
        if errors:
            result["errors"] = list(errors)
        return result

    def _stop_sync_component(
        self,
        name: str,
        component: Any,
        errors: list[dict[str, str]],
    ) -> bool:
        try:
            return self.shutdown_component(component)
        except Exception as exc:
            self._record_error(errors, name, exc)
            return False

    async def _stop_async_component(
        self,
        name: str,
        component: Any,
        errors: list[dict[str, str]],
    ) -> bool:
        try:
            return await self.ashutdown_component(component)
        except Exception as exc:
            self._record_error(errors, name, exc)
            return False

    @staticmethod
    def _record_error(
        errors: list[dict[str, str]],
        component: str,
        exc: Exception,
    ) -> None:
        errors.append(
            {
                "component": component,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        )

    @staticmethod
    def _running_loop() -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    @staticmethod
    def _current_task() -> asyncio.Task[Any] | None:
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    def _reject_reentrant_wait(
        self,
        attempt: _ShutdownAttempt,
        *,
        caller_task: asyncio.Task[Any] | None,
        async_wait: bool,
    ) -> None:
        same_thread = attempt.owner_thread_id == threading.get_ident()
        same_task = caller_task is not None and caller_task is attempt.owner_task
        different_async_task = (
            async_wait
            and same_thread
            and caller_task is not None
            and attempt.owner_task is not None
            and not same_task
        )
        inherited_attempt = self._shutdown_attempt_context.get() is attempt
        if inherited_attempt or same_task or (same_thread and not different_async_task):
            raise RuntimeError(
                "reentrant runtime shutdown cannot wait for its own shutdown attempt"
            )

    def _complete_attempt(
        self,
        attempt: _ShutdownAttempt,
        *,
        result: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        with self._lock:
            attempt.result = dict(result) if result is not None else None
            attempt.error = error
            if self._active_attempt is attempt:
                self._active_attempt = None
            attempt.done.set()

    @staticmethod
    def _attempt_result(attempt: _ShutdownAttempt) -> dict[str, Any]:
        if attempt.error is not None:
            raise attempt.error
        if attempt.result is None:
            raise RuntimeError("runtime shutdown attempt completed without an outcome")
        return dict(attempt.result)

    @staticmethod
    def shutdown_component(component: Any) -> bool:
        if component is None:
            return True
        shutdown = getattr(component, "shutdown", None)
        if callable(shutdown):
            return shutdown() is not False
        close = getattr(component, "close", None)
        if callable(close):
            close()
        return True

    @staticmethod
    async def ashutdown_component(component: Any) -> bool:
        if component is None:
            return True
        ashutdown = getattr(component, "ashutdown", None)
        if callable(ashutdown):
            result = ashutdown()
            if inspect.isawaitable(result):
                result = await result
            return result is not False
        aclose = getattr(component, "aclose", None)
        if callable(aclose):
            result = aclose()
            if inspect.isawaitable(result):
                await result
            return True
        return RuntimeLifecycle.shutdown_component(component)


__all__ = ["RuntimeLifecycle"]

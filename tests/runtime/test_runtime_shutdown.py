import asyncio
import threading
import time

import pytest
from tempfile import TemporaryDirectory
from pathlib import Path
from agent_libos.models import EventType, ProcessStatus
from agent_libos.runtime.runtime import Runtime
from agent_libos.runtime.object_tasks import ObjectTaskManager
from agent_libos.modules import RuntimeModuleRegistry
from agent_libos.sdk import ProtectedOperationSDK

class TestRuntimeShutdown:

    def test_recovery_hooks_worker_and_open_are_strictly_ordered(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        order: list[tuple[str, str, bool]] = []
        original_recover = ProtectedOperationSDK.recover_prepared
        original_hooks = RuntimeModuleRegistry.run_startup_hooks
        original_start = ObjectTaskManager.start_worker

        def recover(sdk: ProtectedOperationSDK) -> object:
            result = original_recover(sdk)
            order.append(('recover', 'recovering', False))
            return result

        def hooks(registry: RuntimeModuleRegistry) -> None:
            lifecycle = registry._hook_services.lifecycle
            # Bind the ordering assertion to the Runtime being assembled.
            # Other Runtime instances in the same xdist worker may still have
            # a legitimately active ObjectTask thread with the same name.
            worker_started = any(item[0] == 'worker' for item in order)
            order.append(('hooks', lifecycle.state, worker_started))
            original_hooks(registry)

        def start(manager: ObjectTaskManager) -> None:
            host = manager._tools._tool_context_host
            order.append(('worker', host.lifecycle.state, manager._started))
            original_start(manager)

        monkeypatch.setattr(ProtectedOperationSDK, 'recover_prepared', recover)
        monkeypatch.setattr(RuntimeModuleRegistry, 'run_startup_hooks', hooks)
        monkeypatch.setattr(ObjectTaskManager, 'start_worker', start)

        unrelated_release = threading.Event()
        unrelated_worker = threading.Thread(
            target=unrelated_release.wait,
            name='agent-libos-object-tasks',
            daemon=True,
        )
        unrelated_worker.start()
        try:
            runtime = Runtime.open('local')
            try:
                assert [item[0] for item in order] == ['recover', 'hooks', 'worker']
                assert order[1] == ('hooks', 'starting', False)
                assert order[2] == ('worker', 'starting', False)
                assert runtime.lifecycle.state == 'open'
                assert runtime.object_tasks._thread.is_alive()
            finally:
                runtime.close()
        finally:
            unrelated_release.set()
            unrelated_worker.join(timeout=1)
            assert not unrelated_worker.is_alive()

    def test_stopping_admission_rejects_mutations_without_writes(self) -> None:
        runtime = Runtime.open('local')
        pid = runtime.process.spawn(goal='admission owner')
        runtime.tools.configure_process_tools(pid, ['get_working_directory'], assigned_by='test')
        entered = threading.Event()
        release = threading.Event()

        def finalizer() -> None:
            entered.set()
            assert release.wait(timeout=2)

        runtime.bind_shutdown_finalizer(finalizer)
        outcome: list[dict[str, object]] = []
        thread = threading.Thread(
            target=lambda: outcome.append(runtime.shutdown(actor='test', reason='admission')),
        )
        thread.start()
        assert entered.wait(timeout=1)
        before = {
            table: len(runtime.store.select_table_rows(table))
            for table in ('processes', 'objects', 'operations', 'audit_records', 'events')
        }

        with pytest.raises(RuntimeError, match='not accepting operations'):
            runtime.process.spawn(goal='rejected spawn')
        with pytest.raises(RuntimeError, match='not accepting operations'):
            runtime.tools.call(pid, 'get_working_directory', {})
        with pytest.raises(RuntimeError, match='not accepting operations'):
            runtime.memory.create_object(pid, 'artifact', {'rejected': True})

        after = {
            table: len(runtime.store.select_table_rows(table))
            for table in before
        }
        assert after == before
        release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert outcome[0]['ok'] is True

    def test_failed_finalizer_retry_resumes_at_first_incomplete_handle(self) -> None:
        runtime = Runtime.open('local')
        calls = {'a': 0, 'b': 0}
        b_succeeds = False

        def finalizer_a() -> bool:
            calls['a'] += 1
            return True

        def finalizer_b() -> bool:
            calls['b'] += 1
            return b_succeeds

        runtime.bind_shutdown_finalizer(finalizer_a)
        runtime.bind_shutdown_finalizer(finalizer_b)

        first = runtime.shutdown(actor='test', reason='retry-finalizer')
        assert first['ok'] is False
        assert runtime.lifecycle.closed is False
        assert runtime.store.list_processes() == []
        assert calls == {'a': 1, 'b': 1}

        b_succeeds = True
        second = runtime.shutdown(actor='test', reason='retry-finalizer')
        assert second['ok'] is True
        assert calls == {'a': 1, 'b': 2}

    def test_store_close_failure_is_reported_and_retried(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = Runtime.open('local')
        original_close = runtime.store.close
        attempts = 0

        def fail_once() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError('injected store close failure')
            original_close()

        monkeypatch.setattr(runtime.store, 'close', fail_once)

        first = runtime.close()
        assert first['ok'] is False
        assert runtime.lifecycle.closed is False
        assert runtime.store.list_processes() == []
        second = runtime.close()
        assert second['ok'] is True
        assert attempts == 2

    def test_sync_shutdown_rejects_async_finalizer_inside_running_loop_before_stopping(self) -> None:
        async def exercise() -> None:
            runtime = Runtime.open('local')

            async def async_finalizer() -> None:
                await asyncio.sleep(0)

            runtime.bind_shutdown_finalizer(async_finalizer)
            with pytest.raises(RuntimeError, match=r'use await runtime\.ashutdown'):
                runtime.shutdown(actor='test', reason='wrong-facade')

            assert runtime.lifecycle.state == 'open'
            pid = runtime.process.spawn(goal='still admitted')
            assert runtime.process.get(pid).status == ProcessStatus.RUNNABLE
            assert (await runtime.ashutdown(actor='test', reason='right-facade'))['ok'] is True

        asyncio.run(exercise())

    def test_cancelled_blocking_wrapper_waits_for_underlying_worker(self) -> None:
        async def exercise() -> None:
            runtime = Runtime.open('local')
            entered = threading.Event()
            release = threading.Event()

            def blocking_work() -> str:
                entered.set()
                assert release.wait(timeout=2)
                return 'done'

            task = asyncio.create_task(runtime.blocking_work.run(blocking_work))
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runtime.blocking_work.active_count() == 0
            assert (await runtime.ashutdown(actor='test', reason='worker-drained'))['ok'] is True

        asyncio.run(exercise())

    def test_close_returns_shutdown_outcome(self) -> None:
        runtime = Runtime.open("local")

        result = runtime.close()

        assert result == {
            "ok": True,
            "already_shutdown": False,
            "reason": "runtime.close",
        }

    def test_concurrent_shutdown_is_single_flight(self) -> None:
        runtime = Runtime.open("local")
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def finalizer() -> None:
            nonlocal calls
            with calls_lock:
                calls += 1
            entered.set()
            assert release.wait(timeout=2.0)

        runtime.bind_shutdown_finalizer(finalizer)
        results: list[dict[str, object]] = []

        def close_runtime() -> None:
            results.append(runtime.shutdown(actor="test", reason="concurrent"))

        first = threading.Thread(target=close_runtime)
        second = threading.Thread(target=close_runtime)
        first.start()
        assert entered.wait(timeout=2.0)
        second.start()
        time.sleep(0.05)
        release.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)

        assert not first.is_alive()
        assert not second.is_alive()
        assert calls == 1
        assert results == [
            {"ok": True, "already_shutdown": False, "reason": "concurrent"},
            {"ok": True, "already_shutdown": False, "reason": "concurrent"},
        ]
        assert runtime.shutdown()["already_shutdown"] is True

    def test_concurrent_async_shutdown_is_single_flight(self) -> None:
        runtime = Runtime.open("local")
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        async def finalizer() -> None:
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.to_thread(release.wait, 2.0)

        runtime.bind_shutdown_finalizer(finalizer)

        async def exercise() -> list[dict[str, object]]:
            first = asyncio.create_task(
                runtime.ashutdown(actor="test", reason="async-concurrent")
            )
            assert await asyncio.to_thread(entered.wait, 2.0)
            second = asyncio.create_task(
                runtime.ashutdown(actor="test", reason="async-concurrent")
            )
            await asyncio.sleep(0.05)
            release.set()
            return await asyncio.gather(first, second)

        results = asyncio.run(exercise())

        assert calls == 1
        assert results == [
            {"ok": True, "already_shutdown": False, "reason": "async-concurrent"},
            {"ok": True, "already_shutdown": False, "reason": "async-concurrent"},
        ]

    def test_reentrant_sync_shutdown_is_rejected_without_deadlock(self) -> None:
        runtime = Runtime.open("local")

        def finalizer() -> None:
            with pytest.raises(RuntimeError, match="reentrant runtime shutdown"):
                runtime.shutdown(actor="nested", reason="nested")

        runtime.bind_shutdown_finalizer(finalizer)

        assert runtime.shutdown(actor="test", reason="outer") == {
            "ok": True,
            "already_shutdown": False,
            "reason": "outer",
        }

    def test_reentrant_async_shutdown_is_rejected_without_deadlock(self) -> None:
        runtime = Runtime.open("local")

        async def finalizer() -> None:
            with pytest.raises(RuntimeError, match="reentrant runtime shutdown"):
                await runtime.ashutdown(actor="nested", reason="nested")

        runtime.bind_shutdown_finalizer(finalizer)

        assert asyncio.run(runtime.ashutdown(actor="test", reason="outer")) == {
            "ok": True,
            "already_shutdown": False,
            "reason": "outer",
        }

    def test_reentrant_async_shutdown_from_child_task_is_rejected(self) -> None:
        runtime = Runtime.open("local")

        async def finalizer() -> None:
            with pytest.raises(RuntimeError, match="reentrant runtime shutdown"):
                await asyncio.create_task(
                    runtime.ashutdown(actor="nested", reason="nested")
                )

        runtime.bind_shutdown_finalizer(finalizer)

        async def exercise() -> dict[str, object]:
            return await asyncio.wait_for(
                runtime.ashutdown(actor="test", reason="outer"),
                timeout=2.0,
            )

        assert asyncio.run(exercise()) == {
            "ok": True,
            "already_shutdown": False,
            "reason": "outer",
        }

    def test_failed_open_does_not_start_object_task_worker(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        existing = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name == "agent-libos-object-tasks"
        }

        def fail_recovery(_store: object, _reason: str) -> list[str]:
            raise RuntimeError("injected object task recovery failure")

        monkeypatch.setattr(
            "agent_libos.storage.sqlite.SQLiteStore.mark_object_tasks_abandoned",
            fail_recovery,
        )

        with pytest.raises(RuntimeError, match="injected object task recovery failure"):
            Runtime.open("local")

        leaked = [
            thread
            for thread in threading.enumerate()
            if thread.name == "agent-libos-object-tasks"
            and thread.ident not in existing
        ]
        assert leaked == []

    def test_shutdown_is_host_lifecycle_not_process_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / 'runtime.sqlite')
            runtime = Runtime.open(db)
            pid = runtime.process.spawn(goal='stay runnable')
            result = runtime.shutdown(actor='test', reason='unit-test')
            assert result['ok']
            assert not result['already_shutdown']
            assert runtime.shutdown()['already_shutdown']
            reopened = Runtime.open(db)
            try:
                process = reopened.store.get_process(pid)
                assert process is not None
                assert process.status == ProcessStatus.RUNNABLE
                assert any((record.action == 'runtime.shutdown' for record in reopened.audit.trace()))
                assert any((event.type == EventType.RUNTIME_SHUTDOWN for event in reopened.events.list()))
            finally:
                reopened.shutdown(actor='test', reason='reopen-cleanup')

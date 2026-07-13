from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from agent_libos import Runtime
from agent_libos.tools.base import SyncAgentTool, ToolContext
from tests.support.skills import write_skill_package


class _EmptyArgs(BaseModel):
    pass


class _ConcurrentRegistrationTool(SyncAgentTool[_EmptyArgs]):
    name = "concurrent_registration_tool"
    description = "Tool used to exercise concurrent registry publication."
    args_schema = _EmptyArgs

    def run(self, args: _EmptyArgs, ctx: ToolContext) -> dict[str, bool]:
        return {"ok": True}


class _ObservedLifecycleLock:
    def __init__(self, delegate: Any, activation_attempted: threading.Event) -> None:
        self._delegate = delegate
        self._activation_attempted = activation_attempted

    def __enter__(self) -> "_ObservedLifecycleLock":
        if threading.current_thread().name == "skill-activation-lock-order":
            self._activation_attempted.set()
        self._delegate.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._delegate.release()


def test_skill_activation_acquires_registry_lifecycle_before_store(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    skill_dir = write_skill_package(tmp_path, "registry-lock-skill", allowed_tools=["echo"])
    runtime = Runtime.open("local")
    registration_holds_lifecycle = threading.Event()
    release_registration = threading.Event()
    activation_attempted_lifecycle = threading.Event()
    activation_entered_store = threading.Event()
    errors: list[tuple[str, BaseException]] = []
    threads: list[threading.Thread] = []
    try:
        pid = runtime.process.spawn(image="base-agent:v0", goal="verify registry lock ordering")
        runtime.skills.register_skill_from_path(skill_dir, actor="test", require_capability=False)

        runtime._registry_lifecycle_lock = _ObservedLifecycleLock(
            runtime._registry_lifecycle_lock,
            activation_attempted_lifecycle,
        )
        original_register_locked = runtime.tools._register_tool_locked
        original_transaction = runtime.store.transaction

        def pause_registration_with_lifecycle_held(*args: Any, **kwargs: Any) -> Any:
            registration_holds_lifecycle.set()
            if not release_registration.wait(timeout=5):
                raise AssertionError("registration was not released by the test")
            return original_register_locked(*args, **kwargs)

        @contextmanager
        def observe_transaction(*args: Any, **kwargs: Any) -> Any:
            with original_transaction(*args, **kwargs) as cur:
                if threading.current_thread().name == "skill-activation-lock-order":
                    activation_entered_store.set()
                yield cur

        monkeypatch.setattr(runtime.tools, "_register_tool_locked", pause_registration_with_lifecycle_held)
        monkeypatch.setattr(runtime.store, "transaction", observe_transaction)

        def capture(label: str, operation: Callable[[], Any]) -> None:
            try:
                operation()
            except BaseException as exc:
                errors.append((label, exc))

        registration = threading.Thread(
            name="tool-registration-lock-order",
            target=capture,
            args=("registration", lambda: runtime.tools.register_tool(_ConcurrentRegistrationTool())),
            daemon=True,
        )
        activation = threading.Thread(
            name="skill-activation-lock-order",
            target=capture,
            args=(
                "activation",
                lambda: runtime.skills.activate_skill(
                    pid,
                    "registry-lock-skill",
                    actor=pid,
                    require_capability=False,
                ),
            ),
            daemon=True,
        )
        threads = [registration, activation]

        registration.start()
        assert registration_holds_lifecycle.wait(timeout=2)
        activation.start()
        assert activation_attempted_lifecycle.wait(timeout=2)

        # At this point registration owns the lifecycle lock. Activation must
        # be waiting for it without already owning the store lock.
        entered_store_before_lifecycle = activation_entered_store.is_set()
        release_registration.set()
        for thread in threads:
            thread.join(timeout=3)

        assert not entered_store_before_lifecycle
        assert not [thread.name for thread in threads if thread.is_alive()]
        assert errors == []
        assert "registry-lock-skill" in runtime.process.get(pid).loaded_skills
        assert runtime.tools.resolve("concurrent_registration_tool").name == "concurrent_registration_tool"
    finally:
        release_registration.set()
        for thread in threads:
            thread.join(timeout=0.1)
        if not any(thread.is_alive() for thread in threads):
            runtime.close()

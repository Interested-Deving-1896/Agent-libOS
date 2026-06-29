from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from agent_libos import Runtime
from agent_libos.config import AgentLibOSConfig, LLMDefaults, LLMProfile
from agent_libos.llm.user_profiles import UserLLMProfileStore, default_user_llm_profiles_path
from agent_libos.models import AgentImage, CapabilityRight, ProcessStatus
from agent_libos.models.exceptions import ValidationError
from agent_libos.storage import SQLiteStore
from tests.support.fakes import RecordingActionClient


def _profile_config() -> AgentLibOSConfig:
    return AgentLibOSConfig(
        llm=LLMDefaults(
            default_profile_id="default",
            profiles={
                "default": LLMProfile(model="default-model"),
                "fast": LLMProfile(model="fast-model", temperature=0.0, max_tokens=128),
                "slow": LLMProfile(model="slow-model", temperature=0.4, max_tokens=256),
                "image-default": LLMProfile(model="image-model"),
                "override": LLMProfile(model="override-model"),
                "parallel": LLMProfile(model="parallel-model", parallel_tool_calls=True),
                "auto-wait": LLMProfile(model="auto-wait-model", auto_wait_on_empty_tool_calls=True),
            },
        )
    )


class TestLLMProfiles:
    def test_different_processes_use_different_profile_clients(self) -> None:
        runtime = Runtime(SQLiteStore(":memory:"), config=_profile_config())
        try:
            fast = RecordingActionClient([{"action": "process_exit", "payload": {"profile": "fast"}}])
            slow = RecordingActionClient([{"action": "process_exit", "payload": {"profile": "slow"}}])
            runtime.llms.set_test_client("fast", fast)
            runtime.llms.set_test_client("slow", slow)

            fast_pid = runtime.process.spawn(image="base-agent:v0", goal="fast", llm_profile_id="fast")
            slow_pid = runtime.process.spawn(image="base-agent:v0", goal="slow", llm_profile_id="slow")

            runtime.run_process_once(fast_pid)
            runtime.run_process_once(slow_pid)

            assert len(fast.user_prompts) == 1
            assert len(slow.user_prompts) == 1
            calls = {call.pid: call for call in runtime.store.list_llm_calls(limit=10)}
            assert calls[fast_pid].request_options["llm_profile_id"] == "fast"
            assert calls[fast_pid].request_options["client_class"] == "RecordingActionClient"
            assert calls[slow_pid].request_options["llm_profile_id"] == "slow"
        finally:
            runtime.close()

    def test_spawn_child_fork_and_exec_profile_selection_rules(self) -> None:
        runtime = Runtime(SQLiteStore(":memory:"), config=_profile_config())
        try:
            runtime.register_image(
                AgentImage(
                    image_id="profile-image:v0",
                    name="profile-image",
                    default_tools=["process_exit"],
                    llm_profile_id="image-default",
                ),
                actor="cli",
            )
            runtime.register_image(
                AgentImage(
                    image_id="next-profile-image:v0",
                    name="next-profile-image",
                    default_tools=["process_exit"],
                    llm_profile_id="image-default",
                ),
                actor="cli",
            )

            from_image = runtime.process.spawn(image="profile-image:v0", goal="image default")
            explicit = runtime.process.spawn(image="profile-image:v0", goal="explicit", llm_profile_id="fast")
            runtime.capability.grant(explicit, "process:spawn", [CapabilityRight.WRITE], issued_by="test")
            runtime.capability.grant(explicit, "image:next-profile-image:v0", [CapabilityRight.READ], issued_by="test")
            runtime.capability.grant(explicit, "image:base-agent:v0", [CapabilityRight.READ], issued_by="test")
            forked = runtime.process.fork(parent=explicit, goal="fork inherits")
            spawned = runtime.spawn_child_process(explicit, "fresh child inherits")

            assert runtime.process.get(from_image).llm_profile_id == "image-default"
            assert runtime.process.get(explicit).llm_profile_id == "fast"
            assert runtime.process.get(forked).llm_profile_id == "fast"
            assert runtime.process.get(spawned).llm_profile_id == "fast"

            runtime.exec_process(explicit, "next-profile-image:v0", goal="exec keeps profile")
            assert runtime.process.get(explicit).llm_profile_id == "fast"

            runtime.capability.grant(explicit, "image:base-agent:v0", [CapabilityRight.READ], issued_by="test")
            runtime.exec_process(explicit, "base-agent:v0", goal="exec override", llm_profile_id="override")
            assert runtime.process.get(explicit).llm_profile_id == "override"
        finally:
            runtime.close()

    def test_unknown_profile_fails_closed_when_llm_quantum_runs(self) -> None:
        runtime = Runtime(SQLiteStore(":memory:"), config=_profile_config())
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="unknown profile", llm_profile_id="missing")

            result = runtime.run_process_once(pid)

            assert result["ok"] is False
            assert "unknown LLM profile" in result["error"]
            assert runtime.process.get(pid).status == ProcessStatus.FAILED
            calls = runtime.store.list_llm_calls(pid=pid)
            assert len(calls) == 1
            assert calls[0].status == "error"
            assert calls[0].request_options["llm_profile_id"] == "missing"
        finally:
            runtime.close()

    def test_process_llm_profile_persists_across_reopen(self) -> None:
        config = _profile_config()
        with tempfile.TemporaryDirectory() as temp_dir:
            db = f"{temp_dir}/runtime.sqlite"
            runtime = Runtime.open(db, config=config)
            try:
                pid = runtime.process.spawn(image="base-agent:v0", goal="persist profile", llm_profile_id="slow")
            finally:
                runtime.close()

            reopened = Runtime.open(db, config=config)
            try:
                assert reopened.process.get(pid).llm_profile_id == "slow"
            finally:
                reopened.close()

    def test_llm_profile_can_override_parallel_tool_calls(self) -> None:
        runtime = Runtime(SQLiteStore(":memory:"), config=_profile_config())
        try:
            default = runtime.llms.resolve("default")
            parallel = runtime.llms.resolve("parallel")

            assert default.parallel_tool_calls is False
            assert parallel.parallel_tool_calls is True
            assert parallel.client.parallel_tool_calls is True
        finally:
            runtime.close()

    def test_llm_profile_can_override_auto_wait_on_empty_tool_calls(self) -> None:
        runtime = Runtime(SQLiteStore(":memory:"), config=_profile_config())
        try:
            default = runtime.llms.resolve("default")
            auto_wait = runtime.llms.resolve("auto-wait")

            assert default.auto_wait_on_empty_tool_calls is False
            assert auto_wait.auto_wait_on_empty_tool_calls is True
        finally:
            runtime.close()

    def test_dynamic_llm_profile_can_be_unregistered(self) -> None:
        runtime = Runtime(SQLiteStore(":memory:"), config=_profile_config())
        try:
            runtime.llms.register_profile("temporary", LLMProfile(model="temporary-model"))
            assert runtime.llms.resolve("temporary").profile.model == "temporary-model"

            runtime.llms.unregister_profile("temporary")

            try:
                runtime.llms.resolve("temporary")
            except ValidationError as exc:
                assert "unknown LLM profile" in str(exc)
            else:
                raise AssertionError("temporary profile should be removed")
        finally:
            runtime.close()

    def test_only_default_profile_inherits_legacy_openai_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.example/v1")
        monkeypatch.setenv("OPENAI_MODEL", "ambient-model")
        monkeypatch.setenv("OPENAI_API_MODE", "chat")
        monkeypatch.setenv("OPENAI_TIMEOUT", "7")
        monkeypatch.setenv("OPENAI_MAX_RETRIES", "5")
        monkeypatch.setenv("OPENAI_STORE", "1")
        monkeypatch.setenv("OPENAI_REASONING_EFFORT", "medium")
        monkeypatch.setenv("OPENAI_VERBOSITY", "high")
        monkeypatch.setenv("OPENAI_PARALLEL_TOOL_CALLS", "1")
        monkeypatch.setenv("AGENT_LIBOS_ALLOW_CUSTOM_LLM_BASE_URL", "1")
        monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")
        monkeypatch.setenv("PROFILE_API_KEY", "profile-key")
        config = AgentLibOSConfig(
            llm=LLMDefaults(
                default_profile_id="default",
                profiles={
                    "default": LLMProfile(),
                    "isolated": LLMProfile(model="isolated-model", api_key_env="PROFILE_API_KEY"),
                },
            )
        )
        runtime = Runtime(SQLiteStore(":memory:"), config=config)
        try:
            default_resolved = runtime.llms.resolve("default")
            default_client = default_resolved.client
            isolated_client = runtime.llms.resolve("isolated").client

            assert default_client.base_url == "https://ambient.example/v1"
            assert default_client.model == "ambient-model"
            assert default_client.api_mode == "chat"
            assert default_client.timeout == 7.0
            assert default_client.max_retries == 5
            assert default_client.store is True
            assert default_client.reasoning_effort == "medium"
            assert default_client.verbosity == "high"
            assert default_client.parallel_tool_calls is True
            assert default_resolved.parallel_tool_calls is True
            assert isolated_client.base_url is None
            assert isolated_client.model == "isolated-model"
            assert isolated_client.api_mode == config.llm.api_mode
            assert isolated_client.timeout == config.llm.timeout_s
            assert isolated_client.max_retries == config.llm.max_retries
            assert isolated_client.store == config.llm.store
            assert isolated_client.reasoning_effort is None
            assert isolated_client.verbosity is None
            assert isolated_client.parallel_tool_calls == config.llm.parallel_tool_calls
            assert isolated_client.api_key == "profile-key"
        finally:
            runtime.close()

    def test_runtime_shutdown_closes_async_llm_clients_inside_running_loop(self) -> None:
        async def run() -> bool:
            runtime = Runtime(SQLiteStore(":memory:"), config=_profile_config())
            client = AsyncCloseOnlyClient()
            runtime.llms.set_test_client("default", client)

            result = runtime.shutdown(actor="test", reason="event-loop-shutdown")

            assert result["ok"] is True
            return client.closed

        assert asyncio.run(run()) is True


class TestUserLLMProfileStore:
    def test_default_user_llm_profile_paths_follow_platform_conventions(self) -> None:
        home = Path("/home/example")
        assert default_user_llm_profiles_path(platform="win32", env={"APPDATA": "C:/Users/example/AppData/Roaming"}, home=home) == Path("C:/Users/example/AppData/Roaming") / "Agent libOS" / "llm-profiles.json"
        assert default_user_llm_profiles_path(platform="darwin", env={}, home=home) == home / "Library" / "Application Support" / "Agent libOS" / "llm-profiles.json"
        assert default_user_llm_profiles_path(platform="linux", env={"XDG_CONFIG_HOME": "/tmp/config"}, home=home) == Path("/tmp/config") / "agent-libos" / "llm-profiles.json"

    def test_user_llm_profile_store_round_trips_non_secret_profile_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "llm-profiles.json"
            store = UserLLMProfileStore(path)

            saved = store.upsert(
                "qwen3.7-max",
                {
                    "profile_id": "qwen3.7-max",
                    "model": "qwen3.7-max",
                    "base_url": "https://qwen.example/v1/",
                    "api_key_env": "QWEN_API_KEY",
                    "api_mode": "chat",
                    "temperature": 0.1,
                    "max_tokens": 8192,
                    "auto_wait_on_empty_tool_calls": True,
                    "allow_custom_base_url": False,
                },
            )
            loaded = UserLLMProfileStore(path).load()

            assert saved.model == "qwen3.7-max"
            assert loaded["qwen3.7-max"].base_url == "https://qwen.example/v1"
            assert loaded["qwen3.7-max"].api_key_env == "QWEN_API_KEY"
            assert loaded["qwen3.7-max"].allow_custom_base_url is False
            assert loaded["qwen3.7-max"].auto_wait_on_empty_tool_calls is True
            persisted = json.loads(path.read_text(encoding="utf-8"))["profiles"]["qwen3.7-max"]
            assert persisted["allow_custom_base_url"] is False
            assert "secret" not in path.read_text(encoding="utf-8")
            assert "api_key" not in persisted

    def test_user_llm_profile_store_rejects_invalid_json_and_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "llm-profiles.json"
            path.write_text("{bad", encoding="utf-8")
            store = UserLLMProfileStore(path)

            try:
                store.load()
            except ValidationError as exc:
                assert "invalid LLM profiles JSON" in str(exc)
            else:
                raise AssertionError("bad JSON should fail closed")

            path.unlink()
            try:
                store.upsert("bad", {"model": "bad", "api_key_env": "BAD_API_KEY", "api_key": "secret"})
            except ValidationError as exc:
                assert "API keys are not accepted" in str(exc)
            else:
                raise AssertionError("raw API keys should be rejected")

    def test_runtime_ashutdown_closes_async_llm_clients(self) -> None:
        async def run() -> bool:
            runtime = Runtime(SQLiteStore(":memory:"), config=_profile_config())
            client = AsyncCloseOnlyClient()
            runtime.llms.set_test_client("default", client)

            result = await runtime.ashutdown(actor="test", reason="async-shutdown")

            assert result["ok"] is True
            return client.closed

        assert asyncio.run(run()) is True


class AsyncCloseOnlyClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True

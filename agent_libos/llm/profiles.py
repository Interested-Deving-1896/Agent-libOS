from __future__ import annotations

import os
import asyncio
import inspect
import threading
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig, LLMProfile
from agent_libos.llm.client import LLMClient, LLMError
from agent_libos.models.exceptions import ValidationError

if TYPE_CHECKING:
    from agent_libos.runtime.runtime import Runtime

_TRUE_VALUES = {"1", "true", "yes", "on"}
_API_MODES = {"auto", "responses", "chat"}


@dataclass(frozen=True)
class ResolvedLLMProfile:
    profile_id: str
    profile: LLMProfile
    client: Any
    temperature: float
    max_tokens: int
    parallel_tool_calls: bool
    auto_wait_on_empty_tool_calls: bool


class LLMProfileRegistry:
    """Host-side resolver for per-process LLM clients.

    Processes persist only a profile id. The profile registry owns the host
    client cache and reads secrets from environment variables when a real
    OpenAI-compatible client is first needed.
    """

    def __init__(self, runtime: "Runtime", *, config: AgentLibOSConfig | None = None):
        self.runtime = runtime
        self.config = config or DEFAULT_CONFIG
        self._profiles: dict[str, LLMProfile] = {}
        self._clients: dict[str, Any] = {}
        self._test_clients: dict[str, Any] = {}

    def register_profile(self, profile_id: str, profile: LLMProfile | dict[str, Any]) -> None:
        selected_id = self._normalize_profile_id(profile_id)
        selected_profile = profile if isinstance(profile, LLMProfile) else LLMProfile(**dict(profile))
        self._validate_profile(selected_id, selected_profile)
        self._profiles[selected_id] = selected_profile
        stale = self._clients.pop(selected_id, None)
        self._shutdown_client(stale)

    def unregister_profile(self, profile_id: str) -> None:
        selected_id = self._normalize_profile_id(profile_id)
        if selected_id not in self._profiles:
            raise ValidationError(f"LLM profile is not dynamically registered: {selected_id}")
        self._profiles.pop(selected_id)
        stale_client = self._clients.pop(selected_id, None)
        stale_test_client = self._test_clients.pop(selected_id, None)
        self._shutdown_client(stale_client)
        if stale_test_client is not stale_client:
            self._shutdown_client(stale_test_client)

    def set_test_client(self, profile_id: str, client: Any) -> None:
        selected_id = self.require_profile_id(profile_id)
        stale = self._test_clients.get(selected_id)
        if stale is not None and stale is not client:
            self._shutdown_client(stale)
        self._test_clients[selected_id] = client

    def clear_test_client(self, profile_id: str) -> None:
        selected_id = self.require_profile_id(profile_id)
        stale = self._test_clients.pop(selected_id, None)
        self._shutdown_client(stale)

    def resolve_for_process(self, pid: str) -> ResolvedLLMProfile:
        process = self.runtime.process.get(pid)
        profile_id = process.llm_profile_id or self.config.llm.default_profile_id
        return self.resolve(profile_id)

    def client_for_process(self, pid: str) -> Any:
        return self.resolve_for_process(pid).client

    def resolve(self, profile_id: str) -> ResolvedLLMProfile:
        selected_id = self.require_profile_id(profile_id)
        profile = self.profile(selected_id)
        client = self._test_clients.get(selected_id)
        if client is None:
            client = self._clients.get(selected_id)
        if client is None:
            client = self._create_client(selected_id, profile)
            self._clients[selected_id] = client
        return ResolvedLLMProfile(
            profile_id=selected_id,
            profile=profile,
            client=client,
            temperature=self.config.llm.temperature if profile.temperature is None else profile.temperature,
            max_tokens=self.config.llm.max_tokens if profile.max_tokens is None else profile.max_tokens,
            parallel_tool_calls=self._resolved_parallel_tool_calls(profile, client),
            auto_wait_on_empty_tool_calls=self._resolved_auto_wait_on_empty_tool_calls(profile),
        )

    @property
    def default_client(self) -> Any:
        return self.resolve(self.config.llm.default_profile_id).client

    def profile(self, profile_id: str) -> LLMProfile:
        selected_id = self._normalize_profile_id(profile_id)
        if selected_id in self._profiles:
            return self._profiles[selected_id]
        profile = self.config.llm.profiles.get(selected_id)
        if profile is None:
            raise ValidationError(f"unknown LLM profile: {selected_id}")
        return profile

    def require_profile_id(self, profile_id: str | None) -> str:
        selected_id = self._normalize_profile_id(profile_id)
        self.profile(selected_id)
        return selected_id

    def shutdown(self) -> None:
        errors: list[str] = []
        for client in self._unique_clients():
            try:
                self._shutdown_client(client)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
        self._clients.clear()
        self._test_clients.clear()
        if errors:
            raise RuntimeError(f"LLM profile shutdown failed: {errors}")

    async def ashutdown(self) -> None:
        errors: list[str] = []
        for client in self._unique_clients():
            try:
                await self._ashutdown_client(client)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
        self._clients.clear()
        self._test_clients.clear()
        if errors:
            raise RuntimeError(f"LLM profile shutdown failed: {errors}")

    def _create_client(self, profile_id: str, profile: LLMProfile) -> LLMClient:
        if profile.kind != "openai_compatible":
            raise ValidationError(f"unsupported LLM profile kind for {profile_id}: {profile.kind}")
        env = dict(os.environ)
        legacy_env = env if self._uses_legacy_openai_env(profile_id) else {}
        api_mode = (profile.api_mode or _optional_env(legacy_env, "OPENAI_API_MODE") or self.config.llm.api_mode).strip().lower()
        if api_mode not in _API_MODES:
            raise LLMError(f"OPENAI_API_MODE must be one of {sorted(_API_MODES)}, got {api_mode!r}")
        return LLMClient(
            base_url=profile.base_url if profile.base_url is not None else _optional_env(legacy_env, "OPENAI_BASE_URL"),
            model=(
                profile.model
                if profile.model is not None
                else _optional_env(legacy_env, "OPENAI_LANGUAGE_MODEL") or _optional_env(legacy_env, "OPENAI_MODEL")
            ),
            api_key=env.get(profile.api_key_env),
            api_key_env=profile.api_key_env,
            timeout=(
                profile.timeout_s
                if profile.timeout_s is not None
                else _float_env(legacy_env, "OPENAI_TIMEOUT", self.config.llm.timeout_s)
            ),
            max_retries=(
                profile.max_retries
                if profile.max_retries is not None
                else _int_env(legacy_env, "OPENAI_MAX_RETRIES", self.config.llm.max_retries)
            ),
            api_mode=api_mode,  # type: ignore[arg-type]
            store=profile.store if profile.store is not None else _bool_env(legacy_env, "OPENAI_STORE", self.config.llm.store),
            reasoning_effort=(
                profile.reasoning_effort
                if profile.reasoning_effort is not None
                else _optional_env(legacy_env, "OPENAI_REASONING_EFFORT")
            ),
            verbosity=profile.verbosity if profile.verbosity is not None else _verbosity_env(legacy_env),
            safety_identifier=self._profile_safety_identifier(profile, legacy_env, env),
            prompt_cache_key=(
                profile.prompt_cache_key
                if profile.prompt_cache_key is not None
                else _optional_env(legacy_env, "OPENAI_PROMPT_CACHE_KEY") or self.config.llm.prompt_cache_key
            ),
            prompt_cache_retention=(
                profile.prompt_cache_retention
                if profile.prompt_cache_retention is not None
                else _prompt_cache_retention_env(legacy_env) or self.config.llm.prompt_cache_retention
            ),
            responses_previous_response_id=(
                profile.responses_previous_response_id
                if profile.responses_previous_response_id is not None
                else _bool_env(
                    legacy_env,
                    "OPENAI_RESPONSES_PREVIOUS_RESPONSE_ID",
                    self.config.llm.responses_previous_response_id,
                )
            ),
            parallel_tool_calls=(
                profile.parallel_tool_calls
                if profile.parallel_tool_calls is not None
                else _bool_env(
                    legacy_env,
                    "OPENAI_PARALLEL_TOOL_CALLS",
                    self.config.llm.parallel_tool_calls,
                )
            ),
            allow_custom_base_url=(
                profile.allow_custom_base_url
                or _bool_env(env, "AGENT_LIBOS_ALLOW_CUSTOM_LLM_BASE_URL", False)
            ),
            defaults=self.config.llm,
        )

    def _resolved_parallel_tool_calls(self, profile: LLMProfile, client: Any) -> bool:
        if profile.parallel_tool_calls is not None:
            return bool(profile.parallel_tool_calls)
        client_value = getattr(client, "parallel_tool_calls", None)
        if client_value is not None:
            return bool(client_value)
        return bool(self.config.llm.parallel_tool_calls)

    def _resolved_auto_wait_on_empty_tool_calls(self, profile: LLMProfile) -> bool:
        if profile.auto_wait_on_empty_tool_calls is not None:
            return bool(profile.auto_wait_on_empty_tool_calls)
        return bool(self.config.llm.auto_wait_on_empty_tool_calls)

    def _profile_safety_identifier(
        self,
        profile: LLMProfile,
        legacy_env: dict[str, str],
        env: dict[str, str],
    ) -> str | None:
        if profile.safety_identifier is not None:
            return profile.safety_identifier
        if profile.safety_identifier_env is not None:
            return _optional_env(env, profile.safety_identifier_env)
        return _optional_env(legacy_env, "OPENAI_SAFETY_IDENTIFIER") or self.config.llm.safety_identifier

    def _validate_profile(self, profile_id: str, profile: LLMProfile) -> None:
        if profile.kind != "openai_compatible":
            raise ValidationError(f"unsupported LLM profile kind for {profile_id}: {profile.kind}")
        if not profile.api_key_env.strip():
            raise ValidationError(f"LLM profile api_key_env must be non-empty: {profile_id}")

    def _normalize_profile_id(self, profile_id: str | None) -> str:
        selected = str(profile_id or "").strip()
        if not selected:
            raise ValidationError("LLM profile id must be a non-empty string")
        return selected

    def _uses_legacy_openai_env(self, profile_id: str) -> bool:
        return profile_id == self.config.llm.default_profile_id

    def _unique_clients(self) -> list[Any]:
        clients: list[Any] = []
        seen: set[int] = set()
        for client in [*self._clients.values(), *self._test_clients.values()]:
            marker = id(client)
            if marker in seen:
                continue
            seen.add(marker)
            clients.append(client)
        return clients

    def _shutdown_client(self, client: Any | None) -> None:
        if client is None:
            return
        shutdown = getattr(client, "shutdown", None)
        if callable(shutdown):
            result = shutdown()
            if inspect.isawaitable(result):
                _run_awaitable_sync(result)
            return
        close = getattr(client, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                _run_awaitable_sync(result)

    async def _ashutdown_client(self, client: Any | None) -> None:
        if client is None:
            return
        ashutdown = getattr(client, "ashutdown", None)
        if callable(ashutdown):
            result = ashutdown()
            if inspect.isawaitable(result):
                await result
            return
        aclose = getattr(client, "aclose", None)
        if callable(aclose):
            result = aclose()
            if inspect.isawaitable(result):
                await result
            return
        shutdown = getattr(client, "shutdown", None)
        if callable(shutdown):
            result = shutdown()
            if inspect.isawaitable(result):
                await result
            return
        close = getattr(client, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


def _optional_env(env: dict[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _bool_env(env: dict[str, str], key: str, default: bool) -> bool:
    value = env.get(key)
    if value is None:
        return default
    return value.strip().lower() in _TRUE_VALUES


def _float_env(env: dict[str, str], key: str, default: float) -> float:
    value = env.get(key)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise LLMError(f"{key} must be a number") from exc


def _int_env(env: dict[str, str], key: str, default: int) -> int:
    value = env.get(key)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise LLMError(f"{key} must be an integer") from exc


def _verbosity_env(env: dict[str, str]) -> str | None:
    value = _optional_env(env, "OPENAI_VERBOSITY")
    if value is None:
        return None
    selected = value.lower()
    if selected not in {"low", "medium", "high"}:
        raise LLMError("OPENAI_VERBOSITY must be one of low, medium, high")
    return selected


def _prompt_cache_retention_env(env: dict[str, str]) -> str | None:
    value = _optional_env(env, "OPENAI_PROMPT_CACHE_RETENTION")
    if value is None:
        return None
    selected = value.lower()
    if selected not in {"in-memory", "24h"}:
        raise LLMError("OPENAI_PROMPT_CACHE_RETENTION must be one of in-memory, 24h")
    return selected


def _run_awaitable_sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(awaitable)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner, name="agent-libos-llm-profile-close", daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")

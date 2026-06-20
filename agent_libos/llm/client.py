from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models.exceptions import LibOSError
from agent_libos.utils.serde import to_jsonable

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_API_MODES = {"auto", "responses", "chat"}
_LLM_DEFAULTS = DEFAULT_CONFIG.llm


class LLMError(LibOSError):
    pass


@dataclass
class LLMCompletion:
    content: str
    tool_calls: list[dict[str, Any]]
    raw: Any | None = None
    api: str | None = None
    response_id: str | None = None
    request_id: str | None = None
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    reasoning: Any | None = None


@dataclass
class LLMClient:
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    timeout: float = _LLM_DEFAULTS.timeout_s
    max_retries: int = _LLM_DEFAULTS.max_retries
    api_mode: Literal["auto", "responses", "chat"] = _LLM_DEFAULTS.api_mode
    store: bool = _LLM_DEFAULTS.store
    reasoning_effort: str | None = None
    verbosity: Literal["low", "medium", "high"] | None = None
    allow_custom_base_url: bool = False
    _client: Any | None = field(default=None, init=False, repr=False)
    _async_client: Any | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_base_url_policy()

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
        *,
        allow_custom_base_url: bool | None = None,
    ) -> "LLMClient":
        env = dict(os.environ)
        if env_path is not None:
            for key, value in read_dotenv(env_path).items():
                env.setdefault(key, value)
        api_mode = env.get("OPENAI_API_MODE", "auto").strip().lower()
        if api_mode not in _API_MODES:
            raise LLMError(f"OPENAI_API_MODE must be one of {sorted(_API_MODES)}, got {api_mode!r}")
        selected_allow_custom_base_url = (
            _bool_env_from(env, "AGENT_LIBOS_ALLOW_CUSTOM_LLM_BASE_URL", default=False)
            if allow_custom_base_url is None
            else allow_custom_base_url
        )
        return cls(
            base_url=env.get("OPENAI_BASE_URL"),
            model=env.get("OPENAI_LANGUAGE_MODEL") or env.get("OPENAI_MODEL"),
            api_key=env.get("OPENAI_API_KEY"),
            timeout=_float_env_from(env, "OPENAI_TIMEOUT", default=_LLM_DEFAULTS.timeout_s),
            max_retries=_int_env_from(env, "OPENAI_MAX_RETRIES", default=_LLM_DEFAULTS.max_retries),
            api_mode=api_mode,  # type: ignore[arg-type]
            store=_bool_env_from(env, "OPENAI_STORE", default=_LLM_DEFAULTS.store),
            reasoning_effort=_optional_env_from(env, "OPENAI_REASONING_EFFORT"),
            verbosity=_verbosity_env_from(env, "OPENAI_VERBOSITY"),
            allow_custom_base_url=selected_allow_custom_base_url,
        )

    def close(self) -> None:
        for attr in ("_client", "_async_client"):
            client = getattr(self, attr)
            if client is None:
                continue
            close = getattr(client, "close", None) or getattr(client, "aclose", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    _run_sync(result)
            setattr(self, attr, None)

    shutdown = close

    def complete(
        self,
        messages: list[dict[str, Any]],
        temperature: float = _LLM_DEFAULTS.temperature,
        max_tokens: int = _LLM_DEFAULTS.max_tokens,
        json_mode: bool = True,
    ) -> str:
        return self.complete_with_metadata(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        ).content

    def complete_with_metadata(
        self,
        messages: list[dict[str, Any]],
        temperature: float = _LLM_DEFAULTS.temperature,
        max_tokens: int = _LLM_DEFAULTS.max_tokens,
        json_mode: bool = True,
    ) -> LLMCompletion:
        return _run_sync(
            self.acomplete_with_metadata(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
        )

    async def acomplete(
        self,
        messages: list[dict[str, Any]],
        temperature: float = _LLM_DEFAULTS.temperature,
        max_tokens: int = _LLM_DEFAULTS.max_tokens,
        json_mode: bool = True,
    ) -> str:
        return (
            await self.acomplete_with_metadata(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
        ).content

    async def acomplete_with_metadata(
        self,
        messages: list[dict[str, Any]],
        temperature: float = _LLM_DEFAULTS.temperature,
        max_tokens: int = _LLM_DEFAULTS.max_tokens,
        json_mode: bool = True,
    ) -> LLMCompletion:
        selected_messages = self._messages_with_json_instruction(messages) if json_mode else messages
        completion = await self._complete_without_tools(
            messages=selected_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        if not completion.content:
            raise LLMError("LLM returned empty content")
        return completion

    def complete_action(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = _LLM_DEFAULTS.temperature,
        max_tokens: int = _LLM_DEFAULTS.max_tokens,
    ) -> LLMCompletion:
        return _run_sync(
            self.acomplete_action(
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        )

    async def acomplete_action(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = _LLM_DEFAULTS.temperature,
        max_tokens: int = _LLM_DEFAULTS.max_tokens,
    ) -> LLMCompletion:
        if self._use_responses_api():
            try:
                return await self._responses_complete_action(messages, tools, temperature, max_tokens)
            except LLMError as exc:
                if self.api_mode != "auto" or not _should_fallback_to_chat(exc.__cause__ or exc):
                    raise
        return await self._chat_complete_action(messages, tools, temperature, max_tokens)

    async def _complete_without_tools(
        self,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMCompletion:
        if self._use_responses_api():
            try:
                return await self._responses_complete(messages, temperature, max_tokens, json_mode)
            except LLMError as exc:
                if self.api_mode != "auto" or not _should_fallback_to_chat(exc.__cause__ or exc):
                    raise
        return await self._chat_complete(messages, temperature, max_tokens, json_mode)

    async def _responses_complete(
        self,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMCompletion:
        payload = self._responses_payload(messages, temperature=temperature, max_tokens=max_tokens)
        if json_mode:
            payload["text"] = self._text_config(json_mode=True)
        response = await self._create_response(payload)
        return self._completion_from_response(response)

    async def _responses_complete_action(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
    ) -> LLMCompletion:
        payload = self._responses_payload(messages, temperature=temperature, max_tokens=max_tokens)
        payload.update(
            {
                "tools": _responses_tools_from_chat_tools(tools),
                "tool_choice": "auto",
                # The libOS executor dispatches one selected action per quantum.
                "parallel_tool_calls": False,
            }
        )
        response = await self._create_response(payload)
        return self._completion_from_response(response)

    async def _chat_complete(
        self,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMCompletion:
        payload = self._chat_payload(messages=messages, temperature=temperature, max_tokens=max_tokens)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        completion = await self._create_chat_completion(payload)
        result = self._completion_from_chat(completion)
        if self._needs_non_thinking_retry(result):
            retry_payload = self._with_enable_thinking(payload, enabled=False)
            completion = await self._create_chat_completion(retry_payload)
            result = self._completion_from_chat(completion)
        if not result.content:
            finish_reason = _first_choice_attr(completion, "finish_reason")
            raise LLMError(f"LLM returned empty content; finish_reason={finish_reason!r}")
        return result

    async def _chat_complete_action(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
    ) -> LLMCompletion:
        payload = self._chat_payload(messages=messages, temperature=temperature, max_tokens=max_tokens)
        payload.update({"tools": tools, "tool_choice": "auto", "parallel_tool_calls": False})
        try:
            completion = await self._create_chat_completion(payload)
        except LLMError as exc:
            # Preserve compatibility with OpenAI-compatible providers that do not
            # implement tool calling. The executor can still parse fallback JSON.
            message = str(exc).lower()
            if "tools" in message or "tool_choice" in message:
                text = await self.acomplete(messages, temperature=temperature, max_tokens=max_tokens, json_mode=False)
                return LLMCompletion(content=text, tool_calls=[], api="chat")
            raise
        result = self._completion_from_chat(completion)
        if self._needs_non_thinking_retry(result):
            completion = await self._create_chat_completion(self._with_enable_thinking(payload, enabled=False))
            result = self._completion_from_chat(completion)
        return result

    def _client_or_raise(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError("The OpenAI Python SDK is not installed. Install it with `pip install openai`.") from exc
        self._client = OpenAI(**self._client_kwargs())
        return self._client

    def _async_client_or_raise(self) -> Any:
        if self._async_client is not None:
            return self._async_client
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise LLMError("The OpenAI Python SDK is not installed. Install it with `pip install openai`.") from exc
        self._async_client = AsyncOpenAI(**self._client_kwargs())
        return self._async_client

    def _client_kwargs(self) -> dict[str, Any]:
        self._validate_base_url_policy()
        if not self.model:
            raise LLMError("OPENAI_LANGUAGE_MODEL or OPENAI_MODEL is not configured")
        if not (self.api_key or os.getenv("OPENAI_API_KEY")):
            raise LLMError("OPENAI_API_KEY is not configured")

        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            # Let the SDK own transient network/rate-limit retry behavior.
            "max_retries": self.max_retries,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return kwargs

    def _responses_payload(self, messages: list[dict[str, Any]], temperature: float, max_tokens: int) -> dict[str, Any]:
        if not self.model:
            raise LLMError("OPENAI_LANGUAGE_MODEL or OPENAI_MODEL is not configured")
        instructions, input_items = _messages_to_responses_parts(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            "max_output_tokens": max_tokens,
            "store": self.store,
            "truncation": "auto",
        }
        if instructions:
            payload["instructions"] = instructions
        if temperature is not None:
            payload["temperature"] = temperature
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        text_config = self._text_config(json_mode=False)
        if text_config:
            payload["text"] = text_config
        extra_body = self._extra_body()
        if extra_body:
            payload["extra_body"] = extra_body
        return payload

    def _chat_payload(self, messages: list[dict[str, Any]], temperature: float, max_tokens: int) -> dict[str, Any]:
        if not self.model:
            raise LLMError("OPENAI_LANGUAGE_MODEL or OPENAI_MODEL is not configured")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }
        if self.store:
            payload["store"] = True
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if self.verbosity:
            payload["verbosity"] = self.verbosity
        extra_body = self._extra_body()
        if extra_body:
            payload["extra_body"] = extra_body
        return payload

    async def _create_response(self, payload: dict[str, Any]) -> Any:
        client = self._async_client_or_raise()
        return await self._call_with_compatibility(client.responses.create, payload, api="responses")

    async def _create_chat_completion(self, payload: dict[str, Any]) -> Any:
        client = self._async_client_or_raise()
        return await self._call_with_compatibility(client.chat.completions.create, payload, api="chat")

    async def _call_with_compatibility(self, create: Any, payload: dict[str, Any], api: str) -> Any:
        request = dict(payload)
        last_error: Exception | None = None
        for _attempt in range(_LLM_DEFAULTS.compatibility_retry_attempts):
            try:
                return await create(**request)
            except Exception as exc:
                if not _is_openai_sdk_error(exc):
                    raise
                last_error = exc
                retry = self._compatibility_retry_payload(request, exc, api=api)
                if retry is None:
                    request_id = getattr(exc, "request_id", None)
                    status_code = getattr(exc, "status_code", None)
                    raise LLMError(
                        f"OpenAI SDK {api} request failed: status={status_code!r} request_id={request_id!r} error={exc}"
                    ) from exc
                request = retry
        raise LLMError(f"OpenAI SDK {api} request failed after compatibility retries: {last_error}") from last_error

    def _compatibility_retry_payload(self, payload: dict[str, Any], exc: Exception, api: str) -> dict[str, Any] | None:
        message = str(exc).lower()
        retry = dict(payload)

        if "enable_thinking" in message and "extra_body" in retry:
            retry.pop("extra_body", None)
            return retry
        if "max_completion_tokens" in message and "max_completion_tokens" in retry:
            retry["max_tokens"] = retry.pop("max_completion_tokens")
            return retry
        if "max_tokens" in message and "max_tokens" in retry:
            retry["max_completion_tokens"] = retry.pop("max_tokens")
            return retry
        if "max_output_tokens" in message and "max_output_tokens" in retry and api == "responses":
            return None
        for key in ("parallel_tool_calls", "response_format", "temperature", "store", "reasoning", "reasoning_effort"):
            if key in message and key in retry:
                retry.pop(key, None)
                return retry
        if "verbosity" in message:
            if "verbosity" in retry:
                retry.pop("verbosity", None)
                return retry
            text = retry.get("text")
            if isinstance(text, dict) and "verbosity" in text:
                updated_text = dict(text)
                updated_text.pop("verbosity", None)
                retry["text"] = updated_text
                return retry
        if ("text" in message or "json_schema" in message or "json_object" in message) and "text" in retry:
            retry.pop("text", None)
            return retry
        if "response_format" in message and "response_format" in retry:
            retry.pop("response_format", None)
            return retry
        return None

    def _completion_from_response(self, response: Any) -> LLMCompletion:
        error = getattr(response, "error", None)
        if error is not None:
            raise LLMError(f"OpenAI response failed: {error}")
        tool_calls: list[dict[str, Any]] = []
        for item in getattr(response, "output", None) or []:
            if _get_attr_or_key(item, "type") != "function_call":
                continue
            tool_calls.append(
                {
                    "id": _get_attr_or_key(item, "id") or _get_attr_or_key(item, "call_id"),
                    "call_id": _get_attr_or_key(item, "call_id"),
                    "name": _get_attr_or_key(item, "name"),
                    "arguments": _get_attr_or_key(item, "arguments") or "{}",
                }
            )
        return LLMCompletion(
            content=self._response_text(response),
            tool_calls=tool_calls,
            raw=response,
            api="responses",
            response_id=getattr(response, "id", None),
            request_id=getattr(response, "_request_id", None),
            model=str(getattr(response, "model", "")) or None,
            usage=_usage_from_response(response),
            reasoning=_reasoning_from_response(response),
        )

    def _completion_from_chat(self, completion: Any) -> LLMCompletion:
        try:
            message = completion.choices[0].message
        except (AttributeError, IndexError) as exc:
            raise LLMError(f"unexpected LLM response shape: {completion}") from exc

        tool_calls: list[dict[str, Any]] = []
        for call in getattr(message, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            if function is None:
                continue
            tool_calls.append(
                {
                    "id": getattr(call, "id", None),
                    "name": getattr(function, "name", None),
                    "arguments": getattr(function, "arguments", "{}"),
                }
            )

        return LLMCompletion(
            content=self._message_content(message),
            tool_calls=tool_calls,
            raw=completion,
            api="chat",
            response_id=getattr(completion, "id", None),
            request_id=getattr(completion, "_request_id", None),
            model=str(getattr(completion, "model", "")) or None,
            usage=_usage_from_response(completion),
            reasoning=_reasoning_from_chat_message(message),
        )

    def _use_responses_api(self) -> bool:
        if self.api_mode == "responses":
            return True
        if self.api_mode == "chat":
            return False
        return self.base_url is None or _is_openai_base_url(self.base_url)

    def _validate_base_url_policy(self) -> None:
        if self.base_url and not _is_openai_base_url(self.base_url) and not self.allow_custom_base_url:
            raise LLMError(
                "OPENAI_BASE_URL points to a custom endpoint; pass allow_custom_base_url=True "
                "or set AGENT_LIBOS_ALLOW_CUSTOM_LLM_BASE_URL=1 in the host environment"
            )

    def _text_config(self, json_mode: bool) -> dict[str, Any]:
        config: dict[str, Any] = {}
        if json_mode:
            config["format"] = {"type": "json_object"}
        if self.verbosity:
            config["verbosity"] = self.verbosity
        return config

    def _extra_body(self) -> dict[str, Any]:
        configured_thinking = os.getenv("OPENAI_ENABLE_THINKING")
        if configured_thinking is None:
            return {}
        return {"enable_thinking": _bool_env_value(configured_thinking)}

    def _needs_non_thinking_retry(self, completion: LLMCompletion) -> bool:
        if completion.tool_calls or completion.content.strip():
            return False
        if os.getenv("OPENAI_ENABLE_THINKING") is not None:
            return False
        if self.base_url is None or _is_openai_base_url(self.base_url):
            return False
        return True

    @staticmethod
    def _with_enable_thinking(payload: dict[str, Any], enabled: bool) -> dict[str, Any]:
        retry = dict(payload)
        extra_body = dict(retry.get("extra_body") or {})
        extra_body["enable_thinking"] = enabled
        retry["extra_body"] = extra_body
        return retry

    @staticmethod
    def _response_text(response: Any) -> str:
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str):
            return output_text
        parts: list[str] = []
        for item in getattr(response, "output", None) or []:
            if _get_attr_or_key(item, "type") != "message":
                continue
            for content in _get_attr_or_key(item, "content") or []:
                if _get_attr_or_key(content, "type") == "output_text":
                    parts.append(str(_get_attr_or_key(content, "text") or ""))
        return "".join(parts)

    @staticmethod
    def _message_content(message: Any) -> str:
        content = getattr(message, "content", None)
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(_content_part_text(part) for part in content)
        return str(content)

    @staticmethod
    def _messages_with_json_instruction(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if _messages_contain_json_instruction(messages):
            return messages
        new_messages = [dict(message) for message in messages]
        json_instruction = _LLM_DEFAULTS.json_instruction
        for msg in new_messages:
            if msg.get("role") in {"system", "developer"}:
                msg["content"] = str(msg.get("content", "")) + f" {json_instruction}"
                return new_messages
        return [{"role": "system", "content": json_instruction}] + new_messages


def _responses_tools_from_chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = function.get("name") if isinstance(function, dict) else None
        if not name:
            continue
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": function.get("description", ""),
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
                # Existing Pydantic-generated schemas are not strict-mode
                # normalized yet, so keep runtime compatibility while preserving
                # JSON-schema argument guidance.
                "strict": False,
            }
        )
    return converted


def _messages_to_responses_parts(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = _message_content_for_search(message)
        if role in {"system", "developer"}:
            if content:
                instructions.append(content)
            continue
        input_items.append(
            {
                "role": "assistant" if role == "assistant" else "user",
                "content": content,
            }
        )
    return ("\n\n".join(instructions) if instructions else None), input_items


def _is_openai_sdk_error(exc: Exception) -> bool:
    try:
        from openai import OpenAIError
    except ImportError:
        return False
    return isinstance(exc, OpenAIError)


def _should_fallback_to_chat(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    message = str(exc).lower()
    if status_code in _LLM_DEFAULTS.fallback_status_codes:
        return True
    return any(
        fragment in message
        for fragment in (
            "responses",
            "unknown url",
            "unsupported endpoint",
            "not found",
            "invalid endpoint",
        )
    )


def _is_openai_base_url(base_url: str) -> bool:
    parsed = urlparse(base_url)
    if parsed.scheme and parsed.scheme != "https":
        return False
    host = parsed.hostname if parsed.scheme else urlparse(f"https://{base_url}").hostname
    return host == "api.openai.com"


def _messages_contain_json_instruction(messages: list[dict[str, Any]]) -> bool:
    return any("json" in _message_content_for_search(message).lower() for message in messages)


def _message_content_for_search(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_content_part_text(part) for part in content)
    return "" if content is None else str(content)


def _content_part_text(part: Any) -> str:
    if isinstance(part, dict):
        return str(part.get("text") or part.get("content") or "")
    return str(getattr(part, "text", getattr(part, "content", part)) or "")


def _get_attr_or_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _first_choice_attr(completion: Any, attr: str) -> Any:
    try:
        return getattr(completion.choices[0], attr, None)
    except (AttributeError, IndexError):
        return None


def _usage_from_response(response: Any) -> dict[str, Any]:
    usage = _get_attr_or_key(response, "usage")
    if usage is None:
        return {}
    jsonable = to_jsonable(usage)
    return jsonable if isinstance(jsonable, dict) else {"raw": jsonable}


def _reasoning_from_response(response: Any) -> Any | None:
    direct = _get_attr_or_key(response, "reasoning")
    if direct is not None:
        return to_jsonable(direct)
    reasoning_items: list[Any] = []
    for item in _get_attr_or_key(response, "output") or []:
        if _get_attr_or_key(item, "type") == "reasoning":
            reasoning_items.append(to_jsonable(item))
    return reasoning_items or None


def _reasoning_from_chat_message(message: Any) -> Any | None:
    for key in ("reasoning", "reasoning_content", "thinking", "thinking_content"):
        value = _get_attr_or_key(message, key)
        if _has_value(value):
            return to_jsonable(value)
    additional = _get_attr_or_key(message, "additional_kwargs")
    if isinstance(additional, dict):
        for key in ("reasoning", "reasoning_content", "thinking", "thinking_content"):
            value = additional.get(key)
            if _has_value(value):
                return to_jsonable(value)
    return None


def _has_value(value: Any) -> bool:
    return value is not None and value != ""


def _bool_env_value(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise LLMError(f"invalid boolean environment value: {value!r}")


def _bool_env(name: str, default: bool) -> bool:
    return _bool_env_from(os.environ, name, default=default)


def _bool_env_from(env: dict[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    return _bool_env_value(value)


def _float_env(name: str, default: float) -> float:
    return _float_env_from(os.environ, name, default=default)


def _float_env_from(env: dict[str, str], name: str, default: float) -> float:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise LLMError(f"{name} must be a float, got {value!r}") from exc


def _int_env(name: str, default: int) -> int:
    return _int_env_from(os.environ, name, default=default)


def _int_env_from(env: dict[str, str], name: str, default: int) -> int:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise LLMError(f"{name} must be an integer, got {value!r}") from exc


def _optional_env(name: str) -> str | None:
    return _optional_env_from(os.environ, name)


def _optional_env_from(env: dict[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _verbosity_env(name: str) -> Literal["low", "medium", "high"] | None:
    return _verbosity_env_from(os.environ, name)


def _verbosity_env_from(env: dict[str, str], name: str) -> Literal["low", "medium", "high"] | None:
    value = _optional_env_from(env, name)
    if value is None:
        return None
    normalized = value.lower()
    if normalized not in {"low", "medium", "high"}:
        raise LLMError(f"{name} must be one of low, medium, high; got {value!r}")
    return normalized  # type: ignore[return-value]


def _run_sync(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise RuntimeError("Cannot use sync LLMClient APIs inside a running event loop. Use async APIs instead.")


def read_dotenv(path: str | Path) -> dict[str, str]:
    env_path = Path(path)
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_dotenv(path: str | Path = ".env") -> None:
    for key, value in read_dotenv(path).items():
        os.environ.setdefault(key, value)

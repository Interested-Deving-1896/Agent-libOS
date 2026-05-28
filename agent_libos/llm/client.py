from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from agent_libos.exceptions import LibOSError


class LLMError(LibOSError):
    pass


@dataclass
class LLMCompletion:
    content: str
    tool_calls: list[dict[str, Any]]
    raw: Any | None = None


@dataclass
class LLMClient:
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    timeout: float = 60.0

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "LLMClient":
        load_dotenv(env_path)
        return cls(
            base_url=os.getenv("OPENAI_CODING_AGENT_BASE_URL") or os.getenv("OPENAI_BASE_URL"),
            model=os.getenv("OPENAI_LANGUAGE_MODEL") or os.getenv("OPENAI_MODEL"),
            api_key=os.getenv("OPENAI_API_KEY"),
        )

    def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 1200,
        json_mode: bool = True,
    ) -> str:
        if not self.base_url:
            raise LLMError("OPENAI_CODING_AGENT_BASE_URL or OPENAI_BASE_URL is not configured")
        if not self.model:
            raise LLMError("OPENAI_LANGUAGE_MODEL or OPENAI_MODEL is not configured")
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is not configured")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
            if os.getenv("OPENAI_ENABLE_THINKING") is None:
                payload["enable_thinking"] = False
        configured_thinking = os.getenv("OPENAI_ENABLE_THINKING")
        if configured_thinking is not None:
            payload["enable_thinking"] = configured_thinking.lower() in {"1", "true", "yes", "on"}
        try:
            return self._post_chat_completions(payload)
        except LLMError as exc:
            message = str(exc)
            if "enable_thinking" in message:
                payload.pop("enable_thinking", None)
                return self._post_chat_completions(payload)
            if json_mode and "response_format" in message:
                payload.pop("response_format", None)
                return self._post_chat_completions(payload)
            raise

    def complete_action(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> LLMCompletion:
        if not self.base_url:
            raise LLMError("OPENAI_CODING_AGENT_BASE_URL or OPENAI_BASE_URL is not configured")
        if not self.model:
            raise LLMError("OPENAI_LANGUAGE_MODEL or OPENAI_MODEL is not configured")
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is not configured")
        try:
            completion = self._complete_action_openai_sdk(messages, tools, temperature, max_tokens)
            if self._needs_non_thinking_retry(completion):
                completion = self._complete_action_openai_sdk(
                    messages,
                    tools,
                    temperature,
                    max_tokens,
                    force_enable_thinking=False,
                )
            return completion
        except ImportError:
            payload = self._action_payload(messages, tools, temperature, max_tokens)
            completion = self._post_chat_completions_payload(payload)
            if self._needs_non_thinking_retry(completion):
                payload = self._action_payload(messages, tools, temperature, max_tokens, force_enable_thinking=False)
                completion = self._post_chat_completions_payload(payload)
            return completion
        except LLMError as exc:
            message = str(exc)
            if "parallel_tool_calls" in message:
                payload = self._action_payload(messages, tools, temperature, max_tokens)
                payload.pop("parallel_tool_calls", None)
                return self._post_chat_completions_payload(payload)
            if "tools" in message or "tool_choice" in message:
                text = self.complete(messages, temperature=temperature, max_tokens=max_tokens, json_mode=False)
                return LLMCompletion(content=text, tool_calls=[])
            raise

    def _complete_action_openai_sdk(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        force_enable_thinking: bool | None = None,
    ) -> LLMCompletion:
        from openai import OpenAI
        from openai import APIError, OpenAIError

        client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)
        kwargs = self._action_payload(messages, tools, temperature, max_tokens, force_enable_thinking=force_enable_thinking)
        try:
            completion = cast(Any, client.chat.completions.create(**kwargs))
        except (APIError, OpenAIError) as exc:
            raise LLMError(f"OpenAI SDK request failed: {exc}") from exc
        message = completion.choices[0].message
        content = message.content or ""
        tool_calls = []
        for call in message.tool_calls or []:
            function = getattr(call, "function", None)
            if function is None:
                continue
            tool_calls.append(
                {
                    "id": call.id,
                    "name": function.name,
                    "arguments": function.arguments,
                }
            )
        return LLMCompletion(content=content, tool_calls=tool_calls, raw=completion)

    def _action_payload(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        force_enable_thinking: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        configured_thinking = os.getenv("OPENAI_ENABLE_THINKING")
        if force_enable_thinking is not None:
            payload["extra_body"] = {"enable_thinking": force_enable_thinking}
        elif configured_thinking is not None:
            payload["extra_body"] = {
                "enable_thinking": configured_thinking.lower() in {"1", "true", "yes", "on"}
            }
        return payload

    def _needs_non_thinking_retry(self, completion: LLMCompletion) -> bool:
        if completion.tool_calls or completion.content.strip():
            return False
        if os.getenv("OPENAI_ENABLE_THINKING") is not None:
            return False
        return True

    def _post_chat_completions(self, payload: dict[str, Any]) -> str:
        if not self.base_url:
            raise LLMError("OPENAI_CODING_AGENT_BASE_URL or OPENAI_BASE_URL is not configured")
        endpoint = f"{self.base_url.rstrip('/')}/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMError(f"LLM HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"LLM request failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM response was not JSON: {exc}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected LLM response shape: {data}") from exc
        if isinstance(content, list):
            text = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        else:
            text = str(content)
        if not text:
            finish_reason = data.get("choices", [{}])[0].get("finish_reason")
            raise LLMError(f"LLM returned empty content; finish_reason={finish_reason!r}")
        return text

    def _post_chat_completions_payload(self, payload: dict[str, Any]) -> LLMCompletion:
        if not self.base_url:
            raise LLMError("OPENAI_CODING_AGENT_BASE_URL or OPENAI_BASE_URL is not configured")
        endpoint = f"{self.base_url.rstrip('/')}/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMError(f"LLM HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"LLM request failed: {exc.reason}") from exc
        message = data["choices"][0]["message"]
        content = message.get("content") or ""
        tool_calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            tool_calls.append(
                {
                    "id": call.get("id"),
                    "name": function.get("name"),
                    "arguments": function.get("arguments"),
                }
            )
        return LLMCompletion(content=content, tool_calls=tool_calls, raw=data)


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

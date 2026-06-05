from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.models.base import PID


@dataclass
class LLMCallRecord:
    call_id: str
    pid: PID | None
    image_id: str | None
    purpose: str
    status: str
    api: str | None = None
    model: str | None = None
    request_id: str | None = None
    response_id: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    request_options: dict[str, Any] = field(default_factory=dict)
    response_content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    reasoning: Any | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    raw_response: Any | None = None
    error: str | None = None
    created_at: str = ""
    completed_at: str | None = None

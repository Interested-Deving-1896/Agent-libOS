from __future__ import annotations

import hashlib
from typing import Any

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models.exceptions import ValidationError
from agent_libos.utils.serde import dumps, to_jsonable

_TOOL_DEFAULTS = DEFAULT_CONFIG.tools

SENSITIVE_OBSERVABILITY_KEYS = frozenset(
    {
        "answer",
        "args",
        "body",
        "content",
        "context",
        "message",
        "metadata",
        "params",
        "payload",
        "question",
        "result",
        "source_code",
        "stderr",
        "stdout",
        "tests",
    }
)


def json_bytes(value: Any) -> bytes:
    return dumps(to_jsonable(value)).encode("utf-8")


def json_size_bytes(value: Any) -> int:
    return len(json_bytes(value))


def ensure_json_size(value: Any, limit_bytes: int, label: str) -> int:
    size = json_size_bytes(value)
    if size > limit_bytes:
        raise ValidationError(f"{label} exceeds {limit_bytes} bytes (got {size})")
    return size


def observation_envelope(value: Any, *, preview_chars: int | None = None) -> dict[str, Any]:
    """Represent a value by bounded preview plus digest.

    The digest is computed over the original JSON value, while the preview is
    always size-bounded. Callers use this for audit/event records where storing
    full tool args, results, syscalls, or model actions would leak secrets or
    amplify storage use.
    """

    selected_preview_chars = (
        _TOOL_DEFAULTS.tool_observability_preview_chars if preview_chars is None else max(0, preview_chars)
    )
    encoded = json_bytes(value)
    text = encoded.decode("utf-8", errors="replace")
    truncated = len(text) > selected_preview_chars
    return {
        "preview": text[:selected_preview_chars],
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
        "truncated": truncated,
    }


def sanitize_for_observability(
    value: Any,
    *,
    preview_chars: int | None = None,
    sensitive_keys: frozenset[str] = SENSITIVE_OBSERVABILITY_KEYS,
) -> dict[str, Any]:
    redacted = _redact_value(value, sensitive_keys=sensitive_keys)
    envelope = observation_envelope(value, preview_chars=preview_chars)
    envelope["preview"] = observation_envelope(redacted, preview_chars=preview_chars)["preview"]
    envelope["redacted"] = redacted != to_jsonable(value)
    return envelope


def _redact_value(value: Any, *, sensitive_keys: frozenset[str]) -> Any:
    jsonable = to_jsonable(value)
    if isinstance(jsonable, dict):
        redacted: dict[str, Any] = {}
        for key, item in jsonable.items():
            key_text = str(key)
            if key_text.lower() in sensitive_keys:
                envelope = observation_envelope(item)
                envelope["preview"] = "[redacted]"
                redacted[key_text] = {"redacted": True, **envelope}
            else:
                redacted[key_text] = _redact_value(item, sensitive_keys=sensitive_keys)
        return redacted
    if isinstance(jsonable, list):
        return [_redact_value(item, sensitive_keys=sensitive_keys) for item in jsonable]
    return jsonable

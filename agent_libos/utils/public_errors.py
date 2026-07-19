from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agent_libos.models.exceptions import ProviderHostError
from agent_libos.utils.ids import new_id


@dataclass(frozen=True, slots=True)
class PublicErrorEnvelope:
    """Model-visible provider failure identity with no provider-authored text."""

    code: str
    error_type: str
    correlation_id: str

    @property
    def message(self) -> str:
        return (
            f"{self.code}: {self.error_type} "
            f"(correlation_id={self.correlation_id})"
        )

    def to_dict(self, *, include_message: bool = False) -> dict[str, str]:
        payload = {
            "code": self.code,
            "error_type": self.error_type,
            "correlation_id": self.correlation_id,
        }
        if include_message:
            payload["message"] = self.message
        return payload

    @classmethod
    def from_error(cls, error: BaseException) -> PublicErrorEnvelope | None:
        if isinstance(error, ProviderHostError):
            return cls(
                code=error.code,
                error_type=error.error_type,
                correlation_id=error.correlation_id,
            )
        if type(error).__name__ == "ProviderEffectNotStarted":
            return cls(
                code="provider_effect_not_started",
                error_type=type(error).__name__,
                correlation_id=new_id("corr"),
            )
        return None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PublicErrorEnvelope | None:
        selected = {
            key: value.get(key)
            for key in ("code", "error_type", "correlation_id")
        }
        if not all(_is_public_identifier(item) for item in selected.values()):
            return None
        return cls(
            code=str(selected["code"]),
            error_type=str(selected["error_type"]),
            correlation_id=str(selected["correlation_id"]),
        )


def provider_error_envelope(error: BaseException) -> dict[str, str] | None:
    """Return a stable public envelope without provider-authored text."""

    envelope = PublicErrorEnvelope.from_error(error)
    return envelope.to_dict(include_message=True) if envelope is not None else None


def provider_error_envelope_from_mapping(
    value: Mapping[str, Any],
) -> dict[str, str] | None:
    """Recover a validated public envelope from an internal protocol frame."""

    envelope = PublicErrorEnvelope.from_mapping(value)
    return envelope.to_dict(include_message=True) if envelope is not None else None


def public_exception_message(error: BaseException) -> str:
    envelope = provider_error_envelope(error)
    return envelope["message"] if envelope is not None else str(error)


def _is_public_identifier(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 256:
        return False
    return all(character.isalnum() or character in "._:-" for character in value)


__all__ = [
    "PublicErrorEnvelope",
    "provider_error_envelope",
    "provider_error_envelope_from_mapping",
    "public_exception_message",
]

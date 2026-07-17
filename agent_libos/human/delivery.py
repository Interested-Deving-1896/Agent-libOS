from __future__ import annotations

from agent_libos.models.exceptions import ValidationError
from agent_libos.substrate import HumanProvider


class HumanDeliveryService:
    """Narrow transport adapter for Human provider I/O."""

    def __init__(self, provider: HumanProvider) -> None:
        self.provider = provider

    def read(self, prompt: str) -> str:
        result = self.provider.read(prompt)
        if not isinstance(result, str):
            raise ValidationError("Human provider returned a non-text response")
        return result

    def write(self, message: str) -> None:
        self.provider.write(message)

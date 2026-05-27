from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class LLMClient:
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None

    @classmethod
    def from_env(cls) -> "LLMClient":
        return cls(
            base_url=os.getenv("OPENAI_CODING_AGENT_BASE_URL"),
            model=os.getenv("OPENAI_LANGUAGE_MODEL"),
            api_key=os.getenv("OPENAI_API_KEY"),
        )

    def complete(self, *_args, **_kwargs) -> str:
        raise NotImplementedError("LLM calls are host-runtime specific in this MVP")


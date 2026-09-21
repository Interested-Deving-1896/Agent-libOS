from pathlib import Path

import pytest

from agent_libos.config import load_config_file


EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "semantic"
    / "external_classifier.yaml"
)


def test_documented_external_classifier_loads_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEMANTIC_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    config = load_config_file(EXAMPLE)

    assert config.semantic.mode == "shadow"
    assert config.semantic.adapter == "external"
    profile_id = config.semantic.external_profile_id
    assert profile_id is not None
    assert profile_id != config.llm.default_profile_id
    profile = config.llm.profiles[profile_id]
    assert profile.api_mode == "responses"
    assert profile.fallback_json_actions is False
    assert profile.store is False
    assert profile.max_retries == 0

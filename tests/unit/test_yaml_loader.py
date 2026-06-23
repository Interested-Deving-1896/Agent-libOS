from __future__ import annotations

import pytest

from agent_libos.models.exceptions import ValidationError
from agent_libos.utils.yaml_loader import load_yaml_mapping


class TestYamlLoader:
    def test_empty_values_use_yaml_null_semantics(self) -> None:
        data = load_yaml_mapping(
            """
empty:
items:
  -
  - name: first
    optional:
inline: {left:, right: [1, true, null]}
""".lstrip()
        )

        assert data["empty"] is None
        assert data["items"] == [None, {"name": "first", "optional": None}]
        assert data["inline"] == {"left": None, "right": [1, True, None]}

    def test_duplicate_mapping_keys_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate YAML key"):
            load_yaml_mapping(
                """
name: first
name: second
""".lstrip()
            )

    def test_duplicate_nested_mapping_keys_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate YAML key"):
            load_yaml_mapping(
                """
items:
  - name: first
    name: second
""".lstrip()
            )

    def test_duplicate_inline_mapping_keys_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate YAML key"):
            load_yaml_mapping("metadata: {role: review, role: audit}\n")

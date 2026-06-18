from __future__ import annotations
import pytest
from agent_libos.llm.tool_protocol import tool_call_to_action

class TestToolProtocol:

    def test_tool_name_wins_over_action_argument(self) -> None:
        action = tool_call_to_action({'name': 'read_directory', 'arguments': '{"action": "delete_directory", "path": "."}'})
        assert action == {'action': 'read_directory', 'path': '.'}

    def test_empty_tool_name_can_use_fallback_action_argument(self) -> None:
        action = tool_call_to_action({'name': '', 'arguments': '{"action": "read_directory", "path": "."}'})
        assert action == {'action': 'read_directory', 'path': '.'}

    def test_empty_tool_name_without_fallback_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            tool_call_to_action({'name': '', 'arguments': '{"path": "."}'})

    @pytest.mark.parametrize('arguments', ([], 0, False))
    def test_falsey_non_object_arguments_are_rejected(self, arguments: object) -> None:
        with pytest.raises(ValueError):
            tool_call_to_action({'name': 'read_directory', 'arguments': arguments})

    def test_none_or_empty_arguments_default_to_empty_object(self) -> None:
        assert tool_call_to_action({'name': 'get_current_time', 'arguments': None}) == {'action': 'get_current_time'}
        assert tool_call_to_action({'name': 'get_current_time', 'arguments': ''}) == {'action': 'get_current_time'}

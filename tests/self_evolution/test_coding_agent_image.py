from __future__ import annotations
import pytest
from agent_libos.images.base_agent import DEFAULT_IMAGES
from agent_libos.llm.prompt import build_system_prompt

class TestCodingAgentImage:

    def test_coding_agent_prompt_guides_practical_tool_use(self) -> None:
        image = DEFAULT_IMAGES['coding-agent:v0']
        prompt = build_system_prompt(image)
        required_phrases = ['practical coding agent', 'Scale the size', 'Adaptive operating loop', 'read_directory', 'create_memory_object', 'create_memory_namespace', 'fork_child_process', 'spawn_child_process', 'list_memory_namespace', 'request_permission', 'load_image_package', 'ask_human', 'parse_pytest_log', 'process_exit', 'Never claim that tests', 'least-privilege permission', 'Do not over-decompose']
        for phrase in required_phrases:
            assert phrase in prompt

    def test_coding_agent_tool_table_covers_repository_workflow(self) -> None:
        image = DEFAULT_IMAGES['coding-agent:v0']
        tools = set(image.default_tools)
        assert {'read_directory', 'read_text_file', 'write_text_file', 'write_directory', 'delete_file', 'delete_directory', 'create_memory_object', 'create_memory_namespace', 'read_memory_object', 'append_memory_object', 'list_memory_namespace', 'create_object_from_file', 'write_object_to_file', 'fork_child_process', 'spawn_child_process', 'exec_process', 'wait_child_process', 'list_child_processes', 'merge_child_memory', 'signal_child_process', 'get_working_directory', 'set_working_directory', 'request_permission', 'load_image_package', 'ask_human', 'human_output', 'get_current_time', 'sleep', 'parse_pytest_log', 'propose_jit_tool', 'validate_jit_tool', 'register_jit_tool'}.issubset(tools)

    def test_coding_agent_defaults_to_read_only_workspace_authority(self) -> None:
        image = DEFAULT_IMAGES['coding-agent:v0']
        capabilities = image.required_capabilities
        assert {'resource': 'human:owner', 'rights': ['write']} in capabilities
        assert {'resource': 'filesystem:workspace:*', 'rights': ['read']} in capabilities
        assert not any(('write' in spec.get('rights', []) for spec in capabilities if spec['resource'].startswith('filesystem:')))
        assert not any(('delete' in spec.get('rights', []) for spec in capabilities if spec['resource'].startswith('filesystem:')))

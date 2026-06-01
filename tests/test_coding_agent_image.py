from __future__ import annotations

import unittest

from agent_libos.images.base_agent import DEFAULT_IMAGES
from agent_libos.llm.prompt import build_system_prompt


class CodingAgentImageTests(unittest.TestCase):
    def test_coding_agent_prompt_guides_practical_tool_use(self) -> None:
        image = DEFAULT_IMAGES["coding-agent:v0"]
        prompt = build_system_prompt(image)

        required_phrases = [
            "practical coding agent",
            "Scale the size",
            "Adaptive operating loop",
            "read_directory",
            "create_memory_object",
            "create_memory_namespace",
            "fork_child_process",
            "spawn_child_process",
            "list_memory_namespace",
            "request_permission",
            "load_image_from_yaml",
            "ask_human",
            "parse_pytest_log",
            "process_exit",
            "Never claim that tests",
            "least-privilege permission",
            "Do not over-decompose",
        ]
        for phrase in required_phrases:
            self.assertIn(phrase, prompt)

    def test_coding_agent_tool_table_covers_repository_workflow(self) -> None:
        image = DEFAULT_IMAGES["coding-agent:v0"]
        tools = set(image.default_tools)

        self.assertTrue(
            {
                "read_directory",
                "read_text_file",
                "write_text_file",
                "write_directory",
                "delete_file",
                "delete_directory",
                "create_memory_object",
                "create_memory_namespace",
                "read_memory_object",
                "append_memory_object",
                "list_memory_namespace",
                "create_object_from_file",
                "write_object_to_file",
                "fork_child_process",
                "spawn_child_process",
                "exec_process",
                "wait_child_process",
                "list_child_processes",
                "merge_child_memory",
                "signal_child_process",
                "get_working_directory",
                "set_working_directory",
                "request_permission",
                "load_image_from_yaml",
                "ask_human",
                "human_output",
                "get_current_time",
                "sleep",
                "parse_pytest_log",
            }.issubset(tools)
        )

    def test_coding_agent_defaults_to_read_only_workspace_authority(self) -> None:
        image = DEFAULT_IMAGES["coding-agent:v0"]
        capabilities = image.required_capabilities

        self.assertIn({"resource": "human:owner", "rights": ["write"]}, capabilities)
        self.assertIn({"resource": "filesystem:workspace:*", "rights": ["read"]}, capabilities)
        self.assertFalse(any("write" in spec.get("rights", []) for spec in capabilities if spec["resource"].startswith("filesystem:")))
        self.assertFalse(any("delete" in spec.get("rights", []) for spec in capabilities if spec["resource"].startswith("filesystem:")))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import unittest
from typing import Any

from agent_libos import Runtime
from agent_libos.llm.client import LLMCompletion
from agent_libos.llm.context_memory import context_object_name
from agent_libos.models import ObjectRight


class LLMContextMemoryTests(unittest.TestCase):
    def test_llm_context_is_process_readable_writable_memory_object(self) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.llm.client = RecordingActionClient(
                [{"action": "create_memory_object", "type": "observation", "payload": {"seen": 1}}]
            )
            pid = runtime.process.spawn(image="base-agent:v0", goal="create context")

            runtime.run_next_process_once()

            name = context_object_name(pid)
            obj = runtime.store.get_object_by_name(name, namespace=runtime.memory.resolve_namespace(pid))
            self.assertIsNotNone(obj)
            assert obj is not None
            self.assertFalse(obj.immutable)
            self.assertEqual(obj.payload["kind"], "llm_context")
            self.assertTrue(runtime.capability.check(pid, f"object:{obj.oid}", ObjectRight.READ))
            self.assertTrue(runtime.capability.check(pid, f"object:{obj.oid}", ObjectRight.WRITE))
            process = runtime.process.get(pid)
            self.assertIn(obj.oid, [handle.oid for handle in process.memory_view.roots])

            read = runtime.tools.call(pid, "read_memory_object", {"name": name})
            appended = runtime.tools.call(
                pid,
                "append_memory_object",
                {"name": name, "entry": {"kind": "agent_note", "text": "keep this in context"}},
            )

            updated = runtime.store.get_object_by_name(name, namespace=runtime.memory.resolve_namespace(pid))
            self.assertTrue(read.ok, read.error)
            self.assertTrue(appended.ok, appended.error)
            self.assertEqual(updated.payload["entries"][-1]["kind"], "agent_note")
        finally:
            runtime.close()

    def test_llm_context_prompt_grows_by_appending_to_preserve_cache_prefix(self) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.llm.client = RecordingActionClient(
                [
                    {"action": "create_memory_object", "type": "observation", "payload": {"step": 1}},
                    {"action": "create_memory_object", "type": "observation", "payload": {"step": 2}},
                ]
            )
            pid = runtime.process.spawn(image="base-agent:v0", goal="append context")

            runtime.run_next_process_once()
            runtime.run_next_process_once()

            first, second = runtime.llm.client.user_prompts
            self.assertIn("Cache strategy: append_only_stable_prefix", first)
            self.assertIn("LLM context object", first)
            self.assertTrue(second.startswith(first))
            context = runtime.store.get_object_by_name(
                context_object_name(pid),
                namespace=runtime.memory.resolve_namespace(pid),
            )
            kinds = [entry["kind"] for entry in context.payload["entries"]]
            self.assertIn("memory_delta", kinds)
            self.assertGreater(len(second), len(first))
        finally:
            runtime.close()

    def test_llm_prompt_lists_only_process_visible_tools(self) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.llm.client = RecordingActionClient(
                [{"action": "process_exit", "payload": {"done": True}}]
            )
            pid = runtime.process.spawn(image="base-agent:v0", goal="exit")

            runtime.run_next_process_once()

            tool_names = {
                tool["function"]["name"]
                for tool in runtime.llm.client.tool_batches[0]
            }
            self.assertIn("process_exit", tool_names)
            self.assertNotIn("read_text_file", tool_names)
            self.assertNotIn("read_text_file", runtime.llm.client.user_prompts[0])
        finally:
            runtime.close()

    def test_llm_retries_malformed_empty_tool_name_once(self) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.llm.client = RecordingActionClient(
                [
                    {"action": "", "path": "."},
                    {"action": "process_exit", "payload": {"done": True}},
                ]
            )
            pid = runtime.process.spawn(image="base-agent:v0", goal="recover malformed action")

            result = runtime.run_next_process_once()

            self.assertTrue(result["ok"])
            self.assertEqual(result["action"]["action"], "process_exit")
            self.assertEqual(len(runtime.llm.client.user_prompts), 2)
            self.assertIn("could not be dispatched", runtime.llm.client.user_prompts[1])
            repairs = [record for record in runtime.audit.trace() if record.action == "llm.action_repair_requested"]
            self.assertEqual(len(repairs), 1)
            assert repairs[0].decision is not None
            self.assertEqual(repairs[0].decision["tool_calls_preview"][0]["name"], "")
            self.assertIn('"path"', repairs[0].decision["tool_calls_preview"][0]["arguments_preview"])
        finally:
            runtime.close()


class RecordingActionClient:
    def __init__(self, actions: list[dict[str, Any]]):
        self.actions = list(actions)
        self.user_prompts: list[str] = []
        self.tool_batches: list[list[dict[str, Any]]] = []

    def complete_action(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMCompletion:
        self.user_prompts.append(str(messages[-1]["content"]))
        self.tool_batches.append(tools)
        action = self.actions.pop(0)
        name = str(action["action"])
        args = {key: value for key, value in action.items() if key != "action"}
        return LLMCompletion(
            content="",
            tool_calls=[{"id": f"context_{len(self.user_prompts)}", "name": name, "arguments": json.dumps(args)}],
        )


if __name__ == "__main__":
    unittest.main()

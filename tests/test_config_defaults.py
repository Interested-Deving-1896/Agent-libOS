from __future__ import annotations

import asyncio
import json
import unittest

from agent_libos.config import AgentLibOSConfig, DEFAULT_CONFIG, RuntimeDefaults
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import CapabilityRight, ProcessStatus
from agent_libos.runtime.runtime import Runtime
from agent_libos.storage import SQLiteStore


class ConfigDefaultsTests(unittest.TestCase):
    def test_runtime_uses_configured_default_quanta(self) -> None:
        config = AgentLibOSConfig(runtime=RuntimeDefaults(run_until_idle_max_quanta=1))
        runtime = Runtime(SQLiteStore(":memory:"), llm_client=ScriptedActionClient(), config=config)
        try:
            pid = runtime.process.spawn(image=config.runtime.default_image_id, goal="two-step process")

            results = asyncio.run(runtime.arun_until_idle())

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["action"]["action"], "create_memory_object")
            self.assertEqual(runtime.process.get(pid).status, ProcessStatus.RUNNABLE)
        finally:
            runtime.close()

    def test_default_images_are_built_from_runtime_config(self) -> None:
        config = AgentLibOSConfig(runtime=RuntimeDefaults(workspace_namespace="repo"))
        runtime = Runtime(SQLiteStore(":memory:"), llm_client=ScriptedActionClient(), config=config)
        try:
            pid = runtime.process.spawn(image=config.runtime.coding_image_id, goal="inspect")

            self.assertEqual(
                runtime.capability.permission_policy(
                    pid,
                    "filesystem:repo:*",
                    CapabilityRight.READ,
                ),
                runtime.capability.ALWAYS_ALLOW,
            )
            self.assertEqual(
                runtime.capability.permission_policy(
                    pid,
                    DEFAULT_CONFIG.runtime.default_human_resource,
                    CapabilityRight.WRITE,
                ),
                runtime.capability.ALWAYS_ALLOW,
            )
        finally:
            runtime.close()


class ScriptedActionClient:
    def __init__(self) -> None:
        self.actions = [
            {
                "action": "create_memory_object",
                "type": "observation",
                "name": "step",
                "payload": {"ok": True},
            },
            {"action": "process_exit", "payload": {"done": True}},
        ]

    def complete_action(self, messages: list[dict[str, str]], tools: list[dict[str, object]]) -> LLMCompletion:
        action = self.actions.pop(0)
        name = str(action["action"])
        args = {key: value for key, value in action.items() if key != "action"}
        return LLMCompletion(
            content="",
            tool_calls=[{"id": f"config_{len(self.actions)}", "name": name, "arguments": json.dumps(args)}],
        )


if __name__ == "__main__":
    unittest.main()

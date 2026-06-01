from __future__ import annotations

import unittest
from pathlib import Path

from agent_libos import Runtime
from agent_libos.models.exceptions import ValidationError


class Stage2SecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Runtime.open("local")

    def tearDown(self) -> None:
        self.runtime.close()

    def test_jit_tool_is_visible_only_to_registering_process(self) -> None:
        owner = self.runtime.process.spawn(image="toolmaker-agent:v0", goal="make parser")
        other = self.runtime.process.spawn(image="toolmaker-agent:v0", goal="unrelated process")
        candidate = self.runtime.tools.propose(
            owner,
            {
                "name": "count_chars",
                "description": "Count characters in text.",
                "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
                "output_schema": {"type": "object"},
            },
            source_code='def run(args):\n    return {"count": len(args.get("text", ""))}',
            tests=[{"args": {"text": "abc"}, "expected": {"count": 3}}],
        )
        validation = self.runtime.tools.validate(candidate)
        self.assertTrue(validation.ok, validation.errors)
        self.runtime.tools.register(owner, candidate)

        owner_schema_names = self._schema_names(owner)
        other_schema_names = self._schema_names(other)
        owner_call = self.runtime.tools.call(owner, "count_chars", {"text": "abcd"})
        other_call = self.runtime.tools.call(other, "count_chars", {"text": "abcd"})

        self.assertIn("count_chars", owner_schema_names)
        self.assertNotIn("count_chars", other_schema_names)
        self.assertTrue(owner_call.ok)
        self.assertEqual(owner_call.payload, {"count": 4})
        self.assertFalse(other_call.ok)
        self.assertIn("not in process tool table", other_call.error or "")

    def test_jit_tool_rejects_dangerous_imports_and_calls(self) -> None:
        pid = self.runtime.process.spawn(image="toolmaker-agent:v0", goal="make unsafe tool")
        candidate = self.runtime.tools.propose(
            pid,
            {
                "name": "unsafe_reader",
                "description": "Unsafe reader.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
            source_code='import os\n\ndef run(args):\n    return {"cwd": os.getcwd(), "data": open("x").read()}',
            tests=[{"args": {}, "expected": {}}],
        )

        validation = self.runtime.tools.validate(candidate)

        self.assertFalse(validation.ok)
        self.assertTrue(any("banned import: os" in error for error in validation.errors))
        self.assertTrue(any("banned call: open" in error for error in validation.errors))

    def test_jit_tool_cannot_shadow_existing_tool_name(self) -> None:
        pid = self.runtime.process.spawn(image="toolmaker-agent:v0", goal="shadow builtin")
        candidate = self.runtime.tools.propose(
            pid,
            {
                "name": "process_exit",
                "description": "Try to shadow a builtin.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
            source_code='def run(args):\n    return {"shadowed": True}',
            tests=[{"args": {}, "expected": {"shadowed": True}}],
        )

        validation = self.runtime.tools.validate(candidate)

        self.assertTrue(validation.ok, validation.errors)
        with self.assertRaises(ValidationError):
            self.runtime.tools.register(pid, candidate)

    def test_builtin_tools_do_not_directly_touch_host_boundaries(self) -> None:
        builtins_dir = Path("agent_libos/tools/builtin")
        forbidden = ["subprocess", "urllib", "socket", "requests"]
        for path in builtins_dir.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, source, f"{path} should not use {token} directly")

    def _schema_names(self, pid: str) -> set[str]:
        return {schema["function"]["name"] for schema in self.runtime.tools.openai_tool_schemas(pid)}


if __name__ == "__main__":
    unittest.main()

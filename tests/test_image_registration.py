from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_libos import AgentImage, Runtime
from agent_libos.models import CapabilityRight, EventType
from agent_libos.models.exceptions import ValidationError
from agent_libos.substrate import LocalResourceProviderSubstrate


class ImageRegistrationTests(unittest.TestCase):
    def test_register_image_primitive_validates_tools_and_emits_audit(self) -> None:
        runtime = Runtime.open("local")
        try:
            image = AgentImage(
                image_id="custom-review:v0",
                name="custom-review",
                system_prompt="Custom review image.",
                default_tools=["read_memory_object", "human_output"],
                safety_profile="review",
            )

            runtime.register_image(image, actor="test")

            self.assertIs(runtime.get_image("custom-review:v0"), image)
            self.assertIn("image.register", [record.action for record in runtime.audit.trace()])
            self.assertIn(EventType.IMAGE_REGISTERED, [event.type for event in runtime.events.list()])
        finally:
            runtime.close()

    def test_register_image_rejects_unknown_default_tool(self) -> None:
        runtime = Runtime.open("local")
        try:
            with self.assertRaises(ValidationError):
                runtime.register_image(
                    {
                        "image_id": "bad-image:v0",
                        "name": "bad-image",
                        "default_tools": ["not_a_real_tool"],
                    },
                    actor="test",
                )
        finally:
            runtime.close()

    def test_load_image_from_yaml_tool_reads_file_and_registers_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "images" / "yaml-agent.yaml"
            manifest.parent.mkdir()
            manifest.write_text(
                """
image:
  image_id: yaml-agent:v0
  name: yaml-agent
  version: v0
  system_prompt: |
    YAML registered image.
    Keep responses concise.
  default_tools:
    - human_output
    - read_memory_object
  context_policy: evidence_first
  safety_profile: yaml-test
  metadata:
    role: test
""".lstrip(),
                encoding="utf-8",
            )
            runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image="review-agent:v0", goal="load image")
                runtime.filesystem.grant_path(pid, "images/yaml-agent.yaml", [CapabilityRight.READ], issued_by="test")
                runtime.image_registry.grant_register(pid, issued_by="test")

                result = runtime.tools.call(pid, "load_image_from_yaml", {"path": "images/yaml-agent.yaml"})

                self.assertTrue(result.ok, result.error)
                self.assertEqual(result.payload["image_id"], "yaml-agent:v0")
                image = runtime.get_image("yaml-agent:v0")
                self.assertEqual(image.system_prompt, "YAML registered image.\nKeep responses concise.\n")
                self.assertEqual(image.default_tools, ["human_output", "read_memory_object"])
                self.assertEqual(image.metadata, {"role": "test"})
            finally:
                runtime.close()

    def test_load_image_from_yaml_tool_requires_image_write_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "yaml-agent.yaml"
            manifest.write_text(
                """
image_id: yaml-agent:v0
name: yaml-agent
default_tools:
  - human_output
""".lstrip(),
                encoding="utf-8",
            )
            runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image="review-agent:v0", goal="load image without authority")
                runtime.filesystem.grant_path(pid, "yaml-agent.yaml", [CapabilityRight.READ], issued_by="test")

                result = runtime.tools.call(pid, "load_image_from_yaml", {"path": "yaml-agent.yaml"})

                self.assertFalse(result.ok)
                self.assertIn("lacks write on image:yaml-agent:v0", result.error or "")
                with self.assertRaises(KeyError):
                    runtime.get_image("yaml-agent:v0")
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()

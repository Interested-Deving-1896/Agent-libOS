from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from agent_libos import Runtime
from agent_libos.models import CapabilityRight, ValidationResult
from agent_libos.tools.sandbox import DenoTypescriptSandbox, SandboxBackend, SyscallHandler


class SWEAgentSkillTests(unittest.TestCase):
    def test_swe_agent_skill_registers_and_loads_without_granting_resource_authority(self) -> None:
        runtime = Runtime.open("local")
        runtime.tools.sandbox = StaticOnlyTypescriptSandbox()
        try:
            manifest = Path("skills/swe_agent.yaml").read_text(encoding="utf-8")
            registered = runtime.register_skill_from_yaml_text(
                manifest,
                actor="test",
                source_type="workspace",
                source="skills/swe_agent.yaml",
            )
            pid = runtime.process.spawn(image="base-agent:v0", goal="fix issue like SWE-Agent")
            runtime.capability.grant(pid, "skill:swe-agent:v0", [CapabilityRight.EXECUTE], issued_by="test")

            loaded = runtime.skills.load_skill(pid, "swe-agent:v0", actor=pid)
            process = runtime.process.get(pid)

            self.assertEqual(registered["skill_id"], "swe-agent:v0")
            for name in ["swe_view", "swe_grep", "swe_edit", "swe_run", "swe_submit"]:
                self.assertIn(name, loaded["jit_tool_ids"])
                self.assertIn(name, process.tool_table)
            self.assertIn("run_shell_command", process.tool_table)
            self.assertFalse(runtime.capability.check(pid, "filesystem:workspace:*", CapabilityRight.READ))
            self.assertFalse(runtime.capability.check(pid, "filesystem:workspace:*", CapabilityRight.WRITE))
            self.assertFalse(runtime.capability.check(pid, "shell:*", CapabilityRight.EXECUTE))
        finally:
            runtime.close()


class StaticOnlyTypescriptSandbox(SandboxBackend):
    language = "typescript"

    def __init__(self) -> None:
        self.checker = DenoTypescriptSandbox(deno_executable="deno")

    def static_check(self, source_code: str) -> ValidationResult:
        return self.checker.static_check(source_code)

    async def arun_source(
        self,
        source_code: str,
        args: dict[str, Any],
        *,
        pid: str | None = None,
        syscall_handler: SyscallHandler | None = None,
        timeout: float | None = None,
    ) -> Any:
        return {"ok": True}

    def run_tests(
        self,
        source_code: str,
        tests: list[dict[str, Any]],
        timeout: float | None = None,
    ) -> ValidationResult:
        validation = self.static_check(source_code)
        if not validation.ok:
            return validation
        return ValidationResult(ok=True, logs="static-only TypeScript validation")

    def metadata_for_source(self, source_code: str) -> dict[str, Any]:
        return {
            "language": "typescript",
            "imports": [],
            "validation": "static-only",
        }


if __name__ == "__main__":
    unittest.main()

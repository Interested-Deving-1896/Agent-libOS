from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_libos import Runtime
from agent_libos.capability.manager import CapabilityManager
from agent_libos.models import CapabilityRight, ProcessStatus, ValidationResult
from agent_libos.models.exceptions import ValidationError
from agent_libos.substrate import LocalResourceProviderSubstrate
from agent_libos.tools.sandbox import DenoTypescriptSandbox, SandboxBackend, SyscallHandler


class FakeDenoSandbox(SandboxBackend):
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
        if "fake:count_chars" in source_code:
            return {"count": len(str(args.get("text", "")))}
        if "fake:read_file" in source_code:
            assert syscall_handler is not None
            return await syscall_handler("filesystem.read_text", {"path": args["path"]})
        if "fake:write_file" in source_code:
            assert syscall_handler is not None
            return await syscall_handler(
                "filesystem.write_text",
                {"path": args["path"], "content": args["content"], "overwrite": True},
            )
        if "fake:exit_after_result" in source_code:
            assert syscall_handler is not None
            await syscall_handler("process.exit", {"payload": {"done": True}})
            return {"returned_after_exit_syscall": True}
        if "fake:exec_after_result" in source_code:
            assert syscall_handler is not None
            await syscall_handler(
                "process.exec",
                {"image": "base-agent:v0", "goal": "exec target", "preserve_memory": True},
            )
            return {"returned_after_exec_syscall": True}
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
        errors: list[str] = []
        for index, test in enumerate(tests, start=1):
            result = self.run_source(source_code, test.get("args", {}))
            if "expected" in test and result != test["expected"]:
                errors.append(f"test {index} expected {test['expected']!r}, got {result!r}")
        return ValidationResult(ok=not errors, errors=errors, logs="fake deno tests")

    def metadata_for_source(self, source_code: str) -> dict[str, Any]:
        return {"language": "typescript", "deno_version": "fake-deno", "imports": []}


class Stage2SecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Runtime.open("local")
        self.runtime.tools.sandbox = FakeDenoSandbox()

    def tearDown(self) -> None:
        self.runtime.close()

    def test_deno_jit_tool_is_visible_only_to_registering_process(self) -> None:
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
            source_code='export function run(args, libos) { /* fake:count_chars */ return {}; }',
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

    def test_deno_jit_syscall_bypasses_tool_table_but_not_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "pkg").mkdir()
            (root / "pkg" / "data.txt").write_text("secret", encoding="utf-8")
            runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(root))
            runtime.tools.sandbox = FakeDenoSandbox()
            try:
                pid = runtime.process.spawn(image="toolmaker-agent:v0", goal="read via syscall")
                runtime.filesystem.grant_directory(pid, "pkg", [CapabilityRight.READ], issued_by="test")
                self.assertNotIn("read_text_file", runtime.process.get(pid).tool_table)
                self.assertTrue(runtime.tools.call(pid, "set_working_directory", {"path": "pkg"}).ok is False)

                candidate = runtime.tools.propose(
                    pid,
                    {"name": "read_via_syscall", "description": "Read file.", "input_schema": {"type": "object"}},
                    source_code='export async function run(args, libos) { /* fake:read_file */ return {}; }',
                )
                self.assertTrue(runtime.tools.validate(candidate).ok)
                runtime.tools.register(pid, candidate)
                result = runtime.tools.call(pid, "read_via_syscall", {"path": "pkg/data.txt"})

                self.assertTrue(result.ok, result.error)
                self.assertEqual(result.payload["content"], "secret")
            finally:
                runtime.close()

    def test_deno_jit_syscall_denies_missing_capability(self) -> None:
        pid = self.runtime.process.spawn(image="toolmaker-agent:v0", goal="read denied")
        candidate = self.runtime.tools.propose(
            pid,
            {"name": "read_denied", "description": "Read file.", "input_schema": {"type": "object"}},
            source_code='export async function run(args, libos) { /* fake:read_file */ return {}; }',
        )
        self.assertTrue(self.runtime.tools.validate(candidate).ok)
        self.runtime.tools.register(pid, candidate)

        result = self.runtime.tools.call(pid, "read_denied", {"path": "README.md"})

        self.assertFalse(result.ok)
        self.assertIn("lacks read", result.error or "")

    def test_deno_jit_human_approval_is_internal_to_syscall(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(root))
            runtime.tools.sandbox = FakeDenoSandbox()
            try:
                pid = runtime.process.spawn(image="toolmaker-agent:v0", goal="write with approval")
                resource = runtime.filesystem.resource_for_path("out.txt")
                runtime.capability.set_permission_policy(
                    pid,
                    resource,
                    [CapabilityRight.WRITE],
                    CapabilityManager.ASK_EACH_TIME,
                    issued_by="test",
                )
                runtime._current_human_auto_approve = True
                candidate = runtime.tools.propose(
                    pid,
                    {"name": "write_via_syscall", "description": "Write file.", "input_schema": {"type": "object"}},
                    source_code='export async function run(args, libos) { /* fake:write_file */ return {}; }',
                )
                self.assertTrue(runtime.tools.validate(candidate).ok)
                runtime.tools.register(pid, candidate)

                result = runtime.tools.call(pid, "write_via_syscall", {"path": "out.txt", "content": "ok"})

                self.assertTrue(result.ok, result.error)
                self.assertEqual((root / "out.txt").read_text(encoding="utf-8"), "ok")
                self.assertIn("human.response", [record.action for record in runtime.audit.trace()])
            finally:
                runtime.close()

    def test_deno_jit_process_exit_is_applied_after_tool_result(self) -> None:
        pid = self.runtime.process.spawn(image="toolmaker-agent:v0", goal="exit after deno result")
        candidate = self.runtime.tools.propose(
            pid,
            {"name": "exit_after_result", "description": "Exit.", "input_schema": {"type": "object"}},
            source_code='export async function run(args, libos) { /* fake:exit_after_result */ return {}; }',
        )
        self.assertTrue(self.runtime.tools.validate(candidate).ok)
        self.runtime.tools.register(pid, candidate)

        result = self.runtime.tools.call(pid, "exit_after_result", {})

        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.payload, {"returned_after_exit_syscall": True})
        self.assertEqual(self.runtime.process.get(pid).status, ProcessStatus.EXITED)

    def test_deno_jit_process_exec_is_applied_after_tool_result(self) -> None:
        pid = self.runtime.process.spawn(image="toolmaker-agent:v0", goal="exec after deno result")
        candidate = self.runtime.tools.propose(
            pid,
            {"name": "exec_after_result", "description": "Exec.", "input_schema": {"type": "object"}},
            source_code='export async function run(args, libos) { /* fake:exec_after_result */ return {}; }',
        )
        self.assertTrue(self.runtime.tools.validate(candidate).ok)
        self.runtime.tools.register(pid, candidate)

        result = self.runtime.tools.call(pid, "exec_after_result", {})

        process = self.runtime.process.get(pid)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.payload, {"returned_after_exec_syscall": True})
        self.assertEqual(process.image_id, "base-agent:v0")
        self.assertEqual(process.status, ProcessStatus.RUNNABLE)

    def test_deno_static_check_rejects_unsafe_typescript(self) -> None:
        checker = DenoTypescriptSandbox(deno_executable="deno")
        validation = checker.static_check(
            'import x from "npm:left-pad";\n'
            'export async function run(args, libos) { Deno.readTextFileSync("x"); return {}; }'
        )

        self.assertFalse(validation.ok)
        self.assertTrue(any("import is not allowed: npm:left-pad" in error for error in validation.errors))
        self.assertTrue(any("dangerous TypeScript API is not allowed: Deno" in error for error in validation.errors))

    def test_deno_static_check_import_allowlist(self) -> None:
        checker = DenoTypescriptSandbox(deno_executable="deno")
        allowed = checker.static_check(
            'import { join } from "jsr:@std/path";\n'
            'export function run(args, libos) { return { path: join("a", "b") }; }'
        )
        denied = checker.static_check(
            'import fs from "node:fs";\n'
            'import x from "https://deno.land/std/path/mod.ts";\n'
            'import y from "file:///tmp/tool.ts";\n'
            'import z from "jsr:@bad/pkg";\n'
            'export function run(args, libos) { return {}; }'
        )

        self.assertTrue(allowed.ok, allowed.errors)
        self.assertFalse(denied.ok)
        self.assertTrue(any("import is not allowed: node:fs" in error for error in denied.errors))
        self.assertTrue(any("import is not allowed: https://deno.land/std/path/mod.ts" in error for error in denied.errors))
        self.assertTrue(any("import is not allowed: file:///tmp/tool.ts" in error for error in denied.errors))
        self.assertTrue(any("JSR package is not in allowlist: @bad/pkg" in error for error in denied.errors))

    @unittest.skipUnless(shutil.which("deno"), "deno not installed")
    def test_real_deno_tool_runs_and_has_no_host_read_permission(self) -> None:
        sandbox = DenoTypescriptSandbox(deno_executable="deno", default_timeout_s=10.0)
        result = sandbox.run_source(
            "export function run(args, libos) { return { doubled: args.value * 2 }; }",
            {"value": 21},
        )
        with self.assertRaises(Exception) as raised:
            sandbox.run_source(
                'export function run(args, libos) { '
                'const d = (globalThis as Record<string, any>)["De" + "no"]; '
                'return d.readTextFileSync("secret.txt"); '
                '}',
                {},
            )

        self.assertEqual(result, {"doubled": 42})
        self.assertTrue("read" in str(raised.exception).lower() or "permission" in str(raised.exception).lower())

    def test_deno_missing_is_clear_validation_error(self) -> None:
        sandbox = DenoTypescriptSandbox(deno_executable="agent-libos-deno-definitely-missing")
        validation = sandbox.run_tests("export function run(args, libos) { return {}; }", [])

        self.assertFalse(validation.ok)
        self.assertTrue(any("Deno executable not found" in error for error in validation.errors))

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
            source_code="export function run(args, libos) { return { shadowed: true }; }",
            tests=[{"args": {}, "expected": {"ok": True}}],
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

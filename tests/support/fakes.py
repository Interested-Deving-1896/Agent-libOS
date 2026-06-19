from __future__ import annotations

import json
from typing import Any

from agent_libos.llm.client import LLMCompletion
from agent_libos.models import ValidationResult
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


class NoSyscallDenoSandbox(DenoTypescriptSandbox):
    def deno_version(self) -> str:
        return "fake-deno"

    def run_source(
        self,
        source_code: str,
        args: dict[str, Any],
        *,
        pid: str | None = None,
        syscall_handler: SyscallHandler | None = None,
        timeout: float | None = None,
    ) -> Any:
        return {"ok": True}


class RecordingActionClient:
    def __init__(self, actions: list[dict[str, Any]]) -> None:
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
            tool_calls=[{"id": f"test_tool_call_{len(self.user_prompts)}", "name": name, "arguments": json.dumps(args)}],
        )

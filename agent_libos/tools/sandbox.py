from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models.exceptions import SandboxError
from agent_libos.models import ValidationResult

_TOOL_DEFAULTS = DEFAULT_CONFIG.tools


class SandboxBackend:
    def static_check(self, source_code: str) -> ValidationResult:
        raise NotImplementedError

    def run_source(self, source_code: str, args: dict[str, Any], timeout: float = _TOOL_DEFAULTS.sandbox_timeout_s) -> Any:
        raise NotImplementedError

    def run_tests(
        self,
        source_code: str,
        tests: list[dict[str, Any]],
        timeout: float = _TOOL_DEFAULTS.sandbox_timeout_s,
    ) -> ValidationResult:
        raise NotImplementedError


class PythonSubprocessSandbox(SandboxBackend):
    """MVP Python sandbox.

    This is not a production security boundary. It gives the ToolBroker a
    replaceable backend and blocks common unsafe imports/calls before running
    code in a temporary working directory with a timeout.
    """

    banned_import_roots = {
        "ctypes",
        "http",
        "importlib",
        "os",
        "pathlib",
        "requests",
        "shutil",
        "socket",
        "subprocess",
        "sysconfig",
        "urllib",
    }
    banned_calls = {"__import__", "compile", "eval", "exec", "input", "open"}

    def __init__(self, default_timeout_s: float = _TOOL_DEFAULTS.sandbox_timeout_s):
        self.default_timeout_s = default_timeout_s

    def static_check(self, source_code: str) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        try:
            tree = ast.parse(source_code)
        except SyntaxError as exc:
            return ValidationResult(ok=False, errors=[f"syntax error: {exc}"])
        has_run = any(isinstance(node, ast.FunctionDef) and node.name == "run" for node in tree.body)
        if not has_run:
            errors.append("tool source must define run(args)")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in self.banned_import_roots:
                        errors.append(f"banned import: {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".", 1)[0]
                if root in self.banned_import_roots:
                    errors.append(f"banned import: {node.module}")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in self.banned_calls:
                    errors.append(f"banned call: {node.func.id}")
        return ValidationResult(ok=not errors, errors=errors, warnings=warnings)

    def run_source(self, source_code: str, args: dict[str, Any], timeout: float | None = None) -> Any:
        validation = self.static_check(source_code)
        if not validation.ok:
            raise SandboxError("; ".join(validation.errors))
        selected_timeout = self.default_timeout_s if timeout is None else timeout
        with tempfile.TemporaryDirectory(prefix="agent_libos_tool_") as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "candidate_tool.py").write_text(source_code, encoding="utf-8")
            (tmp_path / "runner.py").write_text(self._runner_source(), encoding="utf-8")
            env = {
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            for key in ("SystemRoot", "PATH"):
                if key in os.environ:
                    env[key] = os.environ[key]
            proc = subprocess.run(
                [sys.executable, "runner.py"],
                cwd=tmp,
                input=json.dumps(args, ensure_ascii=True),
                text=True,
                capture_output=True,
                timeout=selected_timeout,
                env=env,
            )
            if proc.returncode != 0:
                raise SandboxError(proc.stderr.strip() or proc.stdout.strip() or f"tool exited {proc.returncode}")
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise SandboxError(f"tool produced invalid JSON: {exc}: {proc.stdout!r}") from exc
            if not payload.get("ok"):
                raise SandboxError(payload.get("error", "tool failed"))
            return payload.get("result")

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
        logs: list[str] = []
        for index, test in enumerate(tests, start=1):
            try:
                result = self.run_source(source_code, test.get("args", {}), timeout=timeout)
            except Exception as exc:
                errors.append(f"test {index} failed to run: {exc}")
                continue
            logs.append(f"test {index} result: {result!r}")
            if "expected" in test and result != test["expected"]:
                errors.append(f"test {index} expected {test['expected']!r}, got {result!r}")
        return ValidationResult(ok=not errors, errors=errors, logs="\n".join(logs))

    def _runner_source(self) -> str:
        return textwrap.dedent(
            """
            import json
            import traceback
            import candidate_tool

            try:
                import sys
                args = json.loads(sys.stdin.read() or "{}")
                result = candidate_tool.run(args)
                print(json.dumps({"ok": True, "result": result}, ensure_ascii=True))
            except Exception as exc:
                print(json.dumps({"ok": False, "error": str(exc), "traceback": traceback.format_exc()}, ensure_ascii=True))
                raise
            """
        ).strip()

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_libos.capability.profiles import SandboxProfileBuilder
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models.exceptions import SandboxError
from agent_libos.models import ValidationResult
from agent_libos.utils.serde import to_jsonable

_TOOL_DEFAULTS = DEFAULT_CONFIG.tools

SyscallHandler = Callable[[str, dict[str, Any]], Any | Awaitable[Any]]


class SandboxBackend:
    language = "typescript"

    def static_check(self, source_code: str) -> ValidationResult:
        raise NotImplementedError

    async def arun_source(
        self,
        source_code: str,
        args: dict[str, Any],
        *,
        pid: str | None = None,
        syscall_handler: SyscallHandler | None = None,
        timeout: float | None = None,
    ) -> Any:
        raise NotImplementedError

    def run_source(
        self,
        source_code: str,
        args: dict[str, Any],
        *,
        pid: str | None = None,
        syscall_handler: SyscallHandler | None = None,
        timeout: float | None = None,
    ) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.arun_source(
                    source_code,
                    args,
                    pid=pid,
                    syscall_handler=syscall_handler,
                    timeout=timeout,
                )
            )
        raise RuntimeError("Cannot call run_source() inside a running event loop. Use await arun_source(...).")

    def run_tests(
        self,
        source_code: str,
        tests: list[dict[str, Any]],
        timeout: float | None = None,
    ) -> ValidationResult:
        raise NotImplementedError

    def metadata_for_source(self, source_code: str) -> dict[str, Any]:
        return {"language": self.language}


class DenoTypescriptSandbox(SandboxBackend):
    """Deno/TypeScript sandbox for Agent-authored tools.

    Candidate tools run as Deno userland programs without host permissions.
    Their only libOS access path is the NDJSON syscall protocol handled by the
    libOS runtime broker over stdin/stdout.
    """

    _IMPORT_RE = re.compile(
        r"^\s*(?:import|export)\s+(?:[^\"']*?\s+from\s+)?[\"']([^\"']+)[\"']",
        re.MULTILINE,
    )
    _SIDE_EFFECT_IMPORT_RE = re.compile(r"^\s*import\s*[\"']([^\"']+)[\"']", re.MULTILINE)
    _RUN_EXPORT_RE = re.compile(r"export\s+(?:async\s+)?function\s+run\s*\(")
    _DANGEROUS_PATTERNS = {
        "Deno": re.compile(r"(?<![\w$])Deno(?![\w$])"),
        "globalThis.Deno": re.compile(r"globalThis\s*\.\s*Deno"),
        "eval": re.compile(r"(?<![\w$])eval\s*\("),
        "Function": re.compile(r"(?<![\w$])Function\s*\("),
        "Worker": re.compile(r"(?<![\w$])Worker\s*\("),
        "WebAssembly": re.compile(r"(?<![\w$])WebAssembly(?![\w$])"),
    }

    def __init__(
        self,
        *,
        deno_executable: str = _TOOL_DEFAULTS.deno_executable,
        default_timeout_s: float = _TOOL_DEFAULTS.deno_timeout_s,
        max_rpc_calls: int = _TOOL_DEFAULTS.deno_max_rpc_calls,
        max_stdout_bytes: int = _TOOL_DEFAULTS.deno_max_stdout_bytes,
        max_stderr_bytes: int = _TOOL_DEFAULTS.deno_max_stderr_bytes,
        jsr_allowlist: tuple[str, ...] = _TOOL_DEFAULTS.deno_jsr_allowlist,
    ) -> None:
        self.deno_executable = deno_executable
        self.default_timeout_s = default_timeout_s
        self.max_rpc_calls = max_rpc_calls
        self.max_stdout_bytes = max_stdout_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.jsr_allowlist = tuple(jsr_allowlist)
        self.profile_builder = SandboxProfileBuilder()

    def static_check(self, source_code: str) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        if not self._RUN_EXPORT_RE.search(source_code):
            errors.append("TypeScript tool source must export function run(args, libos)")
        if re.search(r"\bimport\s*\(", source_code):
            errors.append("dynamic import() is not allowed")
        for label, pattern in self._DANGEROUS_PATTERNS.items():
            if pattern.search(source_code):
                errors.append(f"dangerous TypeScript API is not allowed: {label}")
        for specifier in self._extract_imports(source_code):
            package = self._jsr_package(specifier)
            if package is None:
                errors.append(f"import is not allowed: {specifier}")
                continue
            if package not in self.jsr_allowlist:
                errors.append(f"JSR package is not in allowlist: {package}")
        return ValidationResult(ok=not errors, errors=errors, warnings=warnings)

    async def arun_source(
        self,
        source_code: str,
        args: dict[str, Any],
        *,
        pid: str | None = None,
        syscall_handler: SyscallHandler | None = None,
        timeout: float | None = None,
    ) -> Any:
        validation = self.static_check(source_code)
        if not validation.ok:
            raise SandboxError("; ".join(validation.errors))
        deno = self._resolve_deno()
        selected_timeout = self.default_timeout_s if timeout is None else timeout
        with tempfile.TemporaryDirectory(prefix="agent_libos_deno_tool_") as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "candidate.ts").write_text(source_code, encoding="utf-8")
            (tmp_path / "runner.ts").write_text(self._runner_source(), encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                deno,
                "run",
                "--no-prompt",
                "runner.ts",
                cwd=tmp,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                return await asyncio.wait_for(
                    self._serve_process(proc, args, syscall_handler),
                    timeout=selected_timeout,
                )
            except TimeoutError as exc:
                await self._kill_process(proc)
                raise TimeoutError(f"Deno JIT tool timed out after {selected_timeout}s") from exc
            except Exception:
                await self._kill_process(proc)
                raise

    def run_tests(
        self,
        source_code: str,
        tests: list[dict[str, Any]],
        timeout: float | None = None,
    ) -> ValidationResult:
        validation = self.static_check(source_code)
        if not validation.ok:
            return validation
        try:
            version = self.deno_version()
        except SandboxError as exc:
            return ValidationResult(ok=False, errors=[str(exc)])
        errors: list[str] = []
        logs: list[str] = [f"language=typescript", f"deno={version}"]
        for index, test in enumerate(tests, start=1):
            syscall_handler, assert_syscalls_consumed = self._test_syscall_handler(test, index)
            try:
                result = self.run_source(
                    source_code,
                    test.get("args", {}),
                    syscall_handler=syscall_handler,
                    timeout=timeout,
                )
                assert_syscalls_consumed()
            except Exception as exc:
                errors.append(f"test {index} failed to run: {exc}")
                continue
            logs.append(f"test {index} result: {result!r}")
            if "expected" in test and result != test["expected"]:
                errors.append(f"test {index} expected {test['expected']!r}, got {result!r}")
        return ValidationResult(ok=not errors, errors=errors, logs="\n".join(logs))

    def metadata_for_source(self, source_code: str) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "language": "typescript",
            "imports": self._extract_imports(source_code),
            "jsr_allowlist": list(self.jsr_allowlist),
            "sandbox_profile": self._profile_json(self.profile_builder.deno_jit()),
        }
        try:
            metadata["deno_version"] = self.deno_version()
        except SandboxError as exc:
            metadata["deno_version_error"] = str(exc)
        return metadata

    def deno_version(self) -> str:
        deno = self._resolve_deno()
        try:
            proc = subprocess.run(
                [deno, "--version"],
                text=True,
                capture_output=True,
                timeout=min(self.default_timeout_s, 5.0),
            )
        except Exception as exc:
            raise SandboxError(f"failed to run Deno executable {deno!r}: {exc}") from exc
        if proc.returncode != 0:
            message = proc.stderr.strip() or proc.stdout.strip() or f"deno exited {proc.returncode}"
            raise SandboxError(message)
        return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else "deno"

    async def _serve_process(
        self,
        proc: asyncio.subprocess.Process,
        args: dict[str, Any],
        syscall_handler: SyscallHandler | None,
    ) -> Any:
        if proc.stdin is None or proc.stdout is None or proc.stderr is None:
            raise SandboxError("Deno process was not created with stdio pipes")
        stderr_task = asyncio.create_task(proc.stderr.read(self.max_stderr_bytes + 1))
        await self._write_frame(proc, {"type": "run", "args": to_jsonable(args)})
        stdout_bytes = 0
        rpc_calls = 0
        while True:
            line = await proc.stdout.readline()
            if not line:
                stderr, _stderr_truncated = await self._finish_stderr(stderr_task)
                code = await proc.wait()
                raise SandboxError(stderr.strip() or f"Deno JIT tool exited before result: {code}")
            stdout_bytes += len(line)
            if stdout_bytes > self.max_stdout_bytes:
                raise SandboxError("Deno JIT stdout exceeded max bytes")
            try:
                frame = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise SandboxError(f"Deno JIT produced non-protocol stdout: {line[:200]!r}") from exc
            frame_type = frame.get("type")
            if frame_type == "syscall":
                rpc_calls += 1
                if rpc_calls > self.max_rpc_calls:
                    await self._write_frame(
                        proc,
                        {
                            "type": "syscall_result",
                            "id": frame.get("id"),
                            "ok": False,
                            "error": f"Deno JIT exceeded max_rpc_calls={self.max_rpc_calls}",
                        },
                    )
                    continue
                await self._handle_syscall_frame(proc, frame, syscall_handler)
                continue
            if frame_type == "result":
                # The result frame is the protocol boundary for a successful
                # tool call. Any remaining Deno event-loop handles belong to
                # this transient tool process and must not delay lifecycle
                # syscalls or scheduler progress.
                value = frame.get("value")
                await self._kill_process(proc)
                stderr, stderr_truncated = await self._finish_stderr(stderr_task)
                if stderr_truncated:
                    raise SandboxError("Deno JIT stderr exceeded max bytes")
                return value
            if frame_type == "error":
                stderr, _stderr_truncated = await self._finish_stderr(stderr_task)
                message = str(frame.get("message") or stderr or "Deno JIT tool failed")
                raise SandboxError(message)
            raise SandboxError(f"unknown Deno JIT protocol frame: {frame_type!r}")

    async def _handle_syscall_frame(
        self,
        proc: asyncio.subprocess.Process,
        frame: dict[str, Any],
        syscall_handler: SyscallHandler | None,
    ) -> None:
        frame_id = frame.get("id")
        if syscall_handler is None:
            await self._write_frame(
                proc,
                {"type": "syscall_result", "id": frame_id, "ok": False, "error": "libOS syscall handler is unavailable"},
            )
            return
        name = str(frame.get("name") or "")
        args = frame.get("args")
        if not isinstance(args, dict):
            args = {}
        try:
            result = syscall_handler(name, args)
            if inspect.isawaitable(result):
                result = await result
            await self._write_frame(
                proc,
                {"type": "syscall_result", "id": frame_id, "ok": True, "payload": to_jsonable(result)},
            )
        except Exception as exc:
            await self._write_frame(
                proc,
                {
                    "type": "syscall_result",
                    "id": frame_id,
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    async def _write_frame(self, proc: asyncio.subprocess.Process, frame: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise SandboxError("Deno process stdin is closed")
        proc.stdin.write((json.dumps(frame, ensure_ascii=True, default=str) + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def _finish_stderr(self, stderr_task: asyncio.Task[bytes]) -> tuple[str, bool]:
        try:
            data = await stderr_task
        except Exception:
            return "", False
        return data[: self.max_stderr_bytes].decode("utf-8", errors="replace"), len(data) > self.max_stderr_bytes

    async def _kill_process(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()

    def _test_syscall_handler(self, test: dict[str, Any], index: int) -> tuple[SyscallHandler, Callable[[], None]]:
        expected = list(test.get("syscalls", []))

        async def handler(name: str, args: dict[str, Any]) -> Any:
            if not expected:
                raise SandboxError(f"test {index} did not expect syscall {name}")
            spec = expected.pop(0)
            expected_name = spec.get("name")
            if expected_name != name:
                raise SandboxError(f"test {index} expected syscall {expected_name}, got {name}")
            if "args" in spec and spec["args"] != args:
                raise SandboxError(f"test {index} syscall {name} expected args {spec['args']!r}, got {args!r}")
            if spec.get("ok", True) is False:
                raise SandboxError(str(spec.get("error", "mock syscall failed")))
            return spec.get("result", spec.get("payload"))

        def assert_consumed() -> None:
            if expected:
                missing = [str(spec.get("name", "<unnamed>")) for spec in expected]
                raise SandboxError(f"test {index} expected syscall(s) not performed: {missing}")

        return handler, assert_consumed

    def _extract_imports(self, source_code: str) -> list[str]:
        imports = [match.group(1) for match in self._IMPORT_RE.finditer(source_code)]
        imports.extend(match.group(1) for match in self._SIDE_EFFECT_IMPORT_RE.finditer(source_code))
        return sorted(set(imports))

    def _jsr_package(self, specifier: str) -> str | None:
        if not specifier.startswith("jsr:"):
            return None
        body = specifier[4:]
        if not body.startswith("@"):
            return None
        parts = body.split("/")
        if len(parts) < 2:
            return None
        scope = parts[0]
        name = parts[1].split("@", 1)[0]
        if not scope or not name:
            return None
        return f"{scope}/{name}"

    def _resolve_deno(self) -> str:
        candidate = self.deno_executable
        if os.path.sep in candidate or (os.path.altsep and os.path.altsep in candidate):
            path = Path(candidate)
            if path.exists():
                return str(path)
        resolved = shutil.which(candidate)
        if resolved is None:
            raise SandboxError(
                f"Deno executable not found: {candidate!r}. Install Deno or configure tools.deno_executable."
            )
        return resolved

    def _profile_json(self, profile: Any) -> dict[str, Any]:
        return {
            "operation": profile.operation,
            "resource": profile.resource,
            "effect": profile.effect.value,
            "risk": profile.risk.value,
            "rule_id": profile.rule_id,
            "restrictions": dict(profile.restrictions),
        }

    def _runner_source(self) -> str:
        return textwrap.dedent(
            """
            import { run } from "./candidate.ts";

            const decoder = new TextDecoder();
            const encoder = new TextEncoder();
            const stdout = Deno.stdout.writable.getWriter();
            const stdin = Deno.stdin.readable.getReader();
            let buffer = "";

            console.log = (...args: unknown[]) => console.error(...args);

            async function readFrame(): Promise<Record<string, unknown>> {
              while (true) {
                const newline = buffer.indexOf("\\n");
                if (newline >= 0) {
                  const line = buffer.slice(0, newline);
                  buffer = buffer.slice(newline + 1);
                  if (line.trim().length === 0) continue;
                  return JSON.parse(line);
                }
                const chunk = await stdin.read();
                if (chunk.done) throw new Error("stdin closed before protocol frame");
                buffer += decoder.decode(chunk.value, { stream: true });
              }
            }

            async function writeFrame(frame: Record<string, unknown>): Promise<void> {
              await stdout.write(encoder.encode(JSON.stringify(frame) + "\\n"));
            }

            const libos = {
              async syscall(name: string, args: Record<string, unknown> = {}): Promise<unknown> {
                const id = crypto.randomUUID();
                await writeFrame({ type: "syscall", id, name, args });
                while (true) {
                  const frame = await readFrame();
                  if (frame.type !== "syscall_result" || frame.id !== id) continue;
                  if (frame.ok) return frame.payload;
                  const error = new Error(String(frame.error ?? "libOS syscall failed"));
                  (error as Error & { details?: unknown }).details = frame;
                  throw error;
                }
              },
            };

            try {
              const frame = await readFrame();
              if (frame.type !== "run") throw new Error("first protocol frame must be run");
              const value = await run(frame.args ?? {}, libos);
              await writeFrame({ type: "result", value });
            } catch (error) {
              await writeFrame({
                type: "error",
                message: error instanceof Error ? error.message : String(error),
                stack: error instanceof Error ? error.stack : undefined,
              });
              Deno.exit(1);
            } finally {
              stdout.releaseLock();
              stdin.releaseLock();
            }
            """
        ).strip()

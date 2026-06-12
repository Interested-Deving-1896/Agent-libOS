from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_libos import Runtime
from agent_libos.api.cli import main as cli_main
from agent_libos.models.exceptions import CapabilityDenied, NotFound, ValidationError
from agent_libos.runtime.syscalls import LibOSSyscallSession
from agent_libos.substrate import LocalResourceProviderSubstrate


class RuntimeModuleTests(unittest.TestCase):
    def test_trusted_startup_module_registers_tool_image_syscall_and_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, source_sha = _write_module(root)
            runtime = Runtime.open(
                module_manifests=(str(manifest),),
                trusted_modules=(f"test-module:v0:{source_sha}",),
            )
            try:
                loaded = runtime.modules.inspect_module("test-module:v0")
                self.assertEqual(loaded["status"], "loaded")
                self.assertIn("module_echo", loaded["registered"]["tools"])
                self.assertIn("module-agent:v0", runtime.images)
                self.assertIsNotNone(runtime.syscalls.get("module.ping"))
                self.assertTrue(any(record.action == "test.startup_hook" for record in runtime.audit.trace()))

                pid = runtime.process.spawn(image="module-agent:v0", goal="use module")
                tool_result = runtime.tools.call(pid, "module_echo", {"text": "hello"})
                self.assertTrue(tool_result.ok)
                self.assertEqual(tool_result.payload["echo"], "hello")

                syscall_result = asyncio.run(LibOSSyscallSession(runtime, pid).handle("module.ping", {"value": "ok"}))
                self.assertEqual(syscall_result["value"], "ok")
                self.assertEqual(syscall_result["pid"], pid)
            finally:
                runtime.close()

    def test_provider_hook_runs_before_runtime_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, source_sha = _write_module(root, provider_hook=True)
            runtime = Runtime.open(
                module_manifests=(str(manifest),),
                trusted_modules=(f"test-module:v0:{source_sha}",),
            )
            try:
                actions = [record.action for record in runtime.audit.trace()]
                self.assertIn("test.provider_hook", actions)
                self.assertIn("module.provider_hook", actions)
            finally:
                runtime.close()

    def test_module_tool_visibility_does_not_grant_filesystem_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "secret.txt").write_text("secret", encoding="utf-8")
            manifest, source_sha = _write_module(root, expose_read_tool=True)
            runtime = Runtime.open(
                substrate=LocalResourceProviderSubstrate(root),
                module_manifests=(str(manifest),),
                trusted_modules=(f"test-module:v0:{source_sha}",),
            )
            try:
                pid = runtime.process.spawn(image="module-agent:v0", goal="read")
                self.assertIn("read_text_file", runtime.process.get(pid).tool_table)
                result = runtime.tools.call(pid, "read_text_file", {"path": "secret.txt"})
                self.assertFalse(result.ok)
                self.assertIn("lacks read", result.error or "")
            finally:
                runtime.close()

    def test_untrusted_module_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest, _source_sha = _write_module(Path(temp_dir))
            with self.assertRaises(CapabilityDenied):
                Runtime.open(module_manifests=(str(manifest),))

    def test_import_string_entrypoint_loads_from_manifest_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, source_sha = _write_module(root, entrypoint="test_module:register_module")
            runtime = Runtime.open(
                module_manifests=(str(manifest),),
                trusted_modules=(f"test-module:v0:{source_sha}",),
            )
            try:
                self.assertIn("test-module:v0", [module["module_id"] for module in runtime.modules.list_modules()])
                self.assertIn("module-agent:v0", runtime.images)
            finally:
                runtime.close()

    def test_source_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, source_sha = _write_module(root)
            text = manifest.read_text(encoding="utf-8").replace(source_sha, "0" * 64)
            manifest.write_text(text, encoding="utf-8")
            with self.assertRaises(ValidationError):
                Runtime.open(
                    module_manifests=(str(manifest),),
                    trusted_modules=(f"test-module:v0:{source_sha}",),
                )

    def test_failed_module_does_not_leave_partial_tool_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, source_sha = _write_module(root, invalid_registration=True)
            runtime = Runtime.open()
            try:
                with self.assertRaises(ValidationError):
                    runtime.modules.load_module_manifest(
                        manifest,
                        trusted_modules=(f"test-module:v0:{source_sha}",),
                    )
                with self.assertRaises(NotFound):
                    runtime.tools.resolve("module_echo")
                failed = runtime.modules.inspect_module("test-module:v0")
                self.assertEqual(failed["status"], "failed")
            finally:
                runtime.close()

    def test_invalid_module_image_does_not_leave_partial_tool_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, source_sha = _write_module(root, invalid_image=True)
            runtime = Runtime.open()
            try:
                with self.assertRaises(ValidationError):
                    runtime.modules.load_module_manifest(
                        manifest,
                        trusted_modules=(f"test-module:v0:{source_sha}",),
                    )
                with self.assertRaises(NotFound):
                    runtime.tools.resolve("module_echo")
            finally:
                runtime.close()

    def test_checkpoint_restore_requires_same_startup_module_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "runtime.sqlite"
            manifest, source_sha = _write_module(root)
            runtime = Runtime.open(
                db,
                module_manifests=(str(manifest),),
                trusted_modules=(f"test-module:v0:{source_sha}",),
            )
            try:
                pid = runtime.process.spawn(image="module-agent:v0", goal="checkpoint")
                checkpoint_id = runtime.checkpoint.create(pid, "module checkpoint", actor=pid)
            finally:
                runtime.close()

            reopened_without_module = Runtime.open(db)
            try:
                with self.assertRaises(ValidationError):
                    reopened_without_module.checkpoint.restore("cli", checkpoint_id, require_capability=False)
            finally:
                reopened_without_module.close()

    def test_cli_modules_verify_list_and_spawn_with_module_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / "runtime.sqlite"
            manifest, source_sha = _write_module(root)
            trust = f"test-module:v0:{source_sha}"

            verified = _run_cli_json(["--db", str(db), "modules", "verify", str(manifest)])
            self.assertFalse(verified["trusted"])

            listed = _run_cli_json(
                [
                    "--db",
                    str(db),
                    "--module-manifest",
                    str(manifest),
                    "--trusted-module",
                    trust,
                    "modules",
                    "list",
                ]
            )
            self.assertIn("test-module:v0", [module["module_id"] for module in listed])

            spawned = _run_cli_json(
                [
                    "--db",
                    str(db),
                    "--module-manifest",
                    str(manifest),
                    "--trusted-module",
                    trust,
                    "spawn",
                    "--image",
                    "module-agent:v0",
                    "--goal",
                    "cli module",
                ]
            )
            self.assertEqual(spawned["image"], "module-agent:v0")


def _write_module(
    root: Path,
    *,
    expose_read_tool: bool = False,
    invalid_registration: bool = False,
    invalid_image: bool = False,
    provider_hook: bool = False,
    entrypoint: str = "./test_module.py:register_module",
) -> tuple[Path, str]:
    source = root / "test_module.py"
    default_tools = ["module_echo", "read_text_file"] if expose_read_tool else ["module_echo"]
    if invalid_image:
        default_tools.append("missing_module_tool")
    provider_hook_code = (
        """
def mark_provider(runtime):
    runtime.audit.record(actor="module:test-module:v0", action="test.provider_hook", target="runtime")
""".rstrip()
        if provider_hook
        else ""
    )
    provider_hook_registration = "ctx.register_provider_hook('test_hook', mark_provider)" if provider_hook else ""
    source.write_text(
        f"""
from pydantic import BaseModel

from agent_libos.models import AgentImage
from agent_libos.tools.base import SyncAgentTool, ToolContext


class EchoArgs(BaseModel):
    text: str


class ModuleEchoTool(SyncAgentTool[EchoArgs]):
    name = "module_echo"
    description = "Echo text through a startup module."
    args_schema = EchoArgs

    def run(self, args: EchoArgs, ctx: ToolContext):
        return {{"echo": args.text, "pid": ctx.pid}}


def module_ping(session, args):
    return {{"pid": session.pid, "value": args.get("value")}}


def mark_startup(runtime):
    runtime.audit.record(actor="module:test-module:v0", action="test.startup_hook", target="runtime")


{provider_hook_code}


def register_module(ctx):
    ctx.register_tool(ModuleEchoTool())
    {"ctx.register_syscall('undeclared.syscall', module_ping)" if invalid_registration else "ctx.register_syscall('module.ping', module_ping)"}
    ctx.register_image(AgentImage(
        image_id="module-agent:v0",
        name="module-agent",
        default_tools={default_tools!r},
    ))
    {provider_hook_registration}
    ctx.add_startup_hook(mark_startup)
""".lstrip(),
        encoding="utf-8",
    )
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    syscalls = "[]\n" if invalid_registration else "['module.ping']\n"
    manifest = root / "module.yaml"
    manifest.write_text(
        f"""
schema_version: 1
module_id: test-module:v0
name: Test startup module
version: v0
entrypoint: {entrypoint}
provides:
  tools: ['module_echo']
  images: ['module-agent:v0']
  syscalls: {syscalls.rstrip()}
  provider_hooks: {["test_hook"] if provider_hook else []!r}
  startup_hooks: ['mark_startup']
sha256: {source_sha}
metadata:
  test: true
""".lstrip(),
        encoding="utf-8",
    )
    return manifest, source_sha


def _run_cli_json(argv: list[str]) -> object:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cli_main(argv)
    return json.loads(buffer.getvalue())


if __name__ == "__main__":
    unittest.main()

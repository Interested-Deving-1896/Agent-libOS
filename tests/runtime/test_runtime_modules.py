from __future__ import annotations
import pytest
import asyncio
import contextlib
import hashlib
import io
import json
import tempfile
from pathlib import Path
from agent_libos import Runtime
from agent_libos.api.cli import main as cli_main
from agent_libos.models.exceptions import CapabilityDenied, NotFound, ValidationError
from agent_libos.runtime.syscalls import LibOSSyscallSession
from agent_libos.substrate import LocalResourceProviderSubstrate

class TestRuntimeModule:

    def test_trusted_startup_module_registers_tool_image_syscall_and_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, source_sha = _write_module(root)
            runtime = Runtime.open(module_manifests=(str(manifest),), trusted_modules=(f'test-module:v0:{source_sha}',))
            try:
                loaded = runtime.modules.inspect_module('test-module:v0')
                assert loaded['status'] == 'loaded'
                assert 'module_echo' in loaded['registered']['tools']
                assert 'module-agent:v0' in runtime.images
                assert runtime.syscalls.get('module.ping') is not None
                assert any((record.action == 'test.startup_hook' for record in runtime.audit.trace()))
                pid = runtime.process.spawn(image='module-agent:v0', goal='use module')
                tool_result = runtime.tools.call(pid, 'module_echo', {'text': 'hello'})
                assert tool_result.ok
                assert tool_result.payload['echo'] == 'hello'
                syscall_result = asyncio.run(LibOSSyscallSession(runtime, pid).handle('module.ping', {'value': 'ok'}))
                assert syscall_result['value'] == 'ok'
                assert syscall_result['pid'] == pid
            finally:
                runtime.close()

    def test_provider_hook_runs_before_runtime_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, source_sha = _write_module(root, provider_hook=True)
            runtime = Runtime.open(module_manifests=(str(manifest),), trusted_modules=(f'test-module:v0:{source_sha}',))
            try:
                actions = [record.action for record in runtime.audit.trace()]
                assert 'test.provider_hook' in actions
                assert 'module.provider_hook' in actions
            finally:
                runtime.close()

    def test_module_tool_visibility_does_not_grant_filesystem_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / 'secret.txt').write_text('secret', encoding='utf-8')
            manifest, source_sha = _write_module(root, expose_read_tool=True)
            runtime = Runtime.open(substrate=LocalResourceProviderSubstrate(root), module_manifests=(str(manifest),), trusted_modules=(f'test-module:v0:{source_sha}',))
            try:
                pid = runtime.process.spawn(image='module-agent:v0', goal='read')
                assert 'read_text_file' in runtime.process.get(pid).tool_table
                result = runtime.tools.call(pid, 'read_text_file', {'path': 'secret.txt'})
                assert not result.ok
                assert 'lacks read' in (result.error or '')
            finally:
                runtime.close()

    def test_untrusted_module_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest, _source_sha = _write_module(Path(temp_dir))
            with pytest.raises(CapabilityDenied):
                Runtime.open(module_manifests=(str(manifest),))

    def test_import_string_entrypoint_loads_from_manifest_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, source_sha = _write_module(root, entrypoint='test_module:register_module')
            runtime = Runtime.open(module_manifests=(str(manifest),), trusted_modules=(f'test-module:v0:{source_sha}',))
            try:
                assert 'test-module:v0' in [module['module_id'] for module in runtime.modules.list_modules()]
                assert 'module-agent:v0' in runtime.images
            finally:
                runtime.close()

    def test_source_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, source_sha = _write_module(root)
            text = manifest.read_text(encoding='utf-8').replace(source_sha, '0' * 64)
            manifest.write_text(text, encoding='utf-8')
            with pytest.raises(ValidationError):
                Runtime.open(module_manifests=(str(manifest),), trusted_modules=(f'test-module:v0:{source_sha}',))

    def test_failed_module_does_not_leave_partial_tool_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, source_sha = _write_module(root, invalid_registration=True)
            runtime = Runtime.open()
            try:
                with pytest.raises(ValidationError):
                    runtime.modules.load_module_manifest(manifest, trusted_modules=(f'test-module:v0:{source_sha}',))
                with pytest.raises(NotFound):
                    runtime.tools.resolve('module_echo')
                failed = runtime.modules.inspect_module('test-module:v0')
                assert failed['status'] == 'failed'
            finally:
                runtime.close()

    def test_invalid_module_image_does_not_leave_partial_tool_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest, source_sha = _write_module(root, invalid_image=True)
            runtime = Runtime.open()
            try:
                with pytest.raises(ValidationError):
                    runtime.modules.load_module_manifest(manifest, trusted_modules=(f'test-module:v0:{source_sha}',))
                with pytest.raises(NotFound):
                    runtime.tools.resolve('module_echo')
            finally:
                runtime.close()

    def test_checkpoint_restore_requires_same_startup_module_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / 'runtime.sqlite'
            manifest, source_sha = _write_module(root)
            runtime = Runtime.open(db, module_manifests=(str(manifest),), trusted_modules=(f'test-module:v0:{source_sha}',))
            try:
                pid = runtime.process.spawn(image='module-agent:v0', goal='checkpoint')
                checkpoint_id = runtime.checkpoint.create(pid, 'module checkpoint', actor=pid)
            finally:
                runtime.close()
            reopened_without_module = Runtime.open(db)
            try:
                with pytest.raises(ValidationError):
                    reopened_without_module.checkpoint.restore('cli', checkpoint_id, require_capability=False)
            finally:
                reopened_without_module.close()

    def test_cli_modules_verify_list_and_spawn_with_module_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / 'runtime.sqlite'
            manifest, source_sha = _write_module(root)
            trust = f'test-module:v0:{source_sha}'
            verified = _run_cli_json(['--db', str(db), 'modules', 'verify', str(manifest)])
            assert not verified['trusted']
            listed = _run_cli_json(['--db', str(db), '--module-manifest', str(manifest), '--trusted-module', trust, 'modules', 'list'])
            assert 'test-module:v0' in [module['module_id'] for module in listed]
            spawned = _run_cli_json(['--db', str(db), '--module-manifest', str(manifest), '--trusted-module', trust, 'spawn', '--image', 'module-agent:v0', '--goal', 'cli module'])
            assert spawned['image'] == 'module-agent:v0'

def _write_module(root: Path, *, expose_read_tool: bool=False, invalid_registration: bool=False, invalid_image: bool=False, provider_hook: bool=False, entrypoint: str='./test_module.py:register_module') -> tuple[Path, str]:
    source = root / 'test_module.py'
    default_tools = ['module_echo', 'read_text_file'] if expose_read_tool else ['module_echo']
    if invalid_image:
        default_tools.append('missing_module_tool')
    provider_hook_code = '\ndef mark_provider(runtime):\n    runtime.audit.record(actor="module:test-module:v0", action="test.provider_hook", target="runtime")\n'.rstrip() if provider_hook else ''
    provider_hook_registration = "ctx.register_provider_hook('test_hook', mark_provider)" if provider_hook else ''
    source.write_text(f"""\nfrom pydantic import BaseModel\n\nfrom agent_libos.models import AgentImage\nfrom agent_libos.tools.base import SyncAgentTool, ToolContext\n\n\nclass EchoArgs(BaseModel):\n    text: str\n\n\nclass ModuleEchoTool(SyncAgentTool[EchoArgs]):\n    name = "module_echo"\n    description = "Echo text through a startup module."\n    args_schema = EchoArgs\n\n    def run(self, args: EchoArgs, ctx: ToolContext):\n        return {{"echo": args.text, "pid": ctx.pid}}\n\n\ndef module_ping(session, args):\n    return {{"pid": session.pid, "value": args.get("value")}}\n\n\ndef mark_startup(runtime):\n    runtime.audit.record(actor="module:test-module:v0", action="test.startup_hook", target="runtime")\n\n\n{provider_hook_code}\n\n\ndef register_module(ctx):\n    ctx.register_tool(ModuleEchoTool())\n    {("ctx.register_syscall('undeclared.syscall', module_ping)" if invalid_registration else "ctx.register_syscall('module.ping', module_ping)")}\n    ctx.register_image(AgentImage(\n        image_id="module-agent:v0",\n        name="module-agent",\n        default_tools={default_tools!r},\n    ))\n    {provider_hook_registration}\n    ctx.add_startup_hook(mark_startup)\n""".lstrip(), encoding='utf-8')
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    syscalls = '[]\n' if invalid_registration else "['module.ping']\n"
    manifest = root / 'module.yaml'
    manifest.write_text(f"\nschema_version: 1\nmodule_id: test-module:v0\nname: Test startup module\nversion: v0\nentrypoint: {entrypoint}\nprovides:\n  tools: ['module_echo']\n  images: ['module-agent:v0']\n  syscalls: {syscalls.rstrip()}\n  provider_hooks: {(['test_hook'] if provider_hook else [])!r}\n  startup_hooks: ['mark_startup']\nsha256: {source_sha}\nmetadata:\n  test: true\n".lstrip(), encoding='utf-8')
    return (manifest, source_sha)

def _run_cli_json(argv: list[str]) -> object:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        cli_main(argv)
    return json.loads(buffer.getvalue())

from __future__ import annotations
import pytest
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from agent_libos import Runtime
from agent_libos.api.cli import main as cli_main
from agent_libos.models import ProcessMessageKind, ProcessStatus
from agent_libos.substrate import LocalResourceProviderSubstrate

class TestCLIBuiltinCommand:

    def test_cli_cd_changes_process_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / 'pkg').mkdir()
            db = root / 'runtime.sqlite'
            with _temporary_cwd(root):
                spawn = _run_cli_json(['--db', str(db), 'spawn', '--image', 'review-agent:v0', '--goal', 'set cwd'])
                result = _run_cli_json(['--db', str(db), 'cd', spawn['pid'], 'pkg'])
            runtime = Runtime.open(db, substrate=LocalResourceProviderSubstrate(root))
            try:
                assert result['pid'] == spawn['pid']
                assert result['working_directory'] == 'pkg'
                assert runtime.process.get(spawn['pid']).working_directory == 'pkg'
            finally:
                runtime.close()

    def test_cli_exit_marks_process_exited_with_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / 'runtime.sqlite'
            with _temporary_cwd(root):
                spawn = _run_cli_json(['--db', str(db), 'spawn', '--image', 'base-agent:v0', '--goal', 'finish'])
                result = _run_cli_json(['--db', str(db), 'exit', spawn['pid'], '--payload', '{"done": true}'])
            runtime = Runtime.open(db, substrate=LocalResourceProviderSubstrate(root))
            try:
                process = runtime.process.get(spawn['pid'])
                assert result['pid'] == spawn['pid']
                assert result['status'] == ProcessStatus.EXITED.value
                assert result['result_oid'] is not None
                assert process.status == ProcessStatus.EXITED
                assert (process.status_message or '').startswith('result_oid:')
            finally:
                runtime.close()

    def test_cli_exec_loads_image_package_from_first_arg_and_uses_second_arg_as_goal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / 'runtime.sqlite'
            package = root / 'cli-image'
            _write_cli_image_package(package)
            with _temporary_cwd(root):
                spawn = _run_cli_json(['--db', str(db), 'spawn', '--image', 'base-agent:v0', '--goal', 'old goal'])
                before = Runtime.open(db, substrate=LocalResourceProviderSubstrate(root))
                try:
                    old_goal_oid = before.process.get(spawn['pid']).goal_oid
                finally:
                    before.close()
                result = _run_cli_json(['--db', str(db), 'exec', str(package), 'new goal from first arg', '--pid', spawn['pid'], '--no-run'])
            runtime = Runtime.open(db, substrate=LocalResourceProviderSubstrate(root))
            try:
                process = runtime.process.get(spawn['pid'])
                assert result['goal'] == 'new goal from first arg'
                assert result['image_arg'] == str(package)
                assert result['loaded_image']['image_id'] == 'cli-package-agent:v0'
                assert result['process']['image'] == 'cli-package-agent:v0'
                assert not result['ran']
                assert process.image_id == 'cli-package-agent:v0'
                assert process.goal_oid != old_goal_oid
                assert 'human_output' in process.tool_table
            finally:
                runtime.close()

    def test_cli_message_and_interrupt_post_human_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db = root / 'runtime.sqlite'
            with _temporary_cwd(root):
                spawn = _run_cli_json(['--db', str(db), 'spawn', '--image', 'base-agent:v0', '--goal', 'listen'])
                normal = _run_cli_json(['--db', str(db), 'message', spawn['pid'], 'please inspect the latest result', '--subject', 'status'])
                interrupt = _run_cli_json(['--db', str(db), 'interrupt', spawn['pid'], 'stop and read this first'])
            runtime = Runtime.open(db, substrate=LocalResourceProviderSubstrate(root))
            try:
                unread = runtime.messages.unread(spawn['pid'])
                assert normal['message']['kind'] == ProcessMessageKind.NORMAL.value
                assert interrupt['message']['kind'] == ProcessMessageKind.INTERRUPT.value
                assert [message.message_id for message in unread] == [normal['message']['message_id'], interrupt['message']['message_id']]
                assert unread[0].sender == 'human:owner'
                assert unread[0].subject == 'status'
                assert unread[1].subject == 'Human interrupt'
            finally:
                runtime.close()

@contextlib.contextmanager
def _temporary_cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)

def _run_cli_json(argv: list[str]) -> dict[str, object]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        cli_main(argv)
    return json.loads(stdout.getvalue())


def _write_cli_image_package(root: Path) -> None:
    root.mkdir(parents=True)
    root.joinpath('IMAGE.yaml').write_text("""
image_id: cli-package-agent:v0
name: cli-package-agent
prompt: prompt.md
default_tools:
  - human_output
context_policy: evidence_first
""".lstrip(), encoding='utf-8')
    root.joinpath('prompt.md').write_text('CLI loaded image.\n', encoding='utf-8')

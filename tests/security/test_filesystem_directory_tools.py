from __future__ import annotations
import pytest
from uuid import uuid4
from agent_libos import Runtime
from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models.exceptions import HumanApprovalRequired, ValidationError
from agent_libos.models import CapabilityRight, HumanRequestStatus
_TOOL_DEFAULTS = DEFAULT_CONFIG.tools
DEFAULT_FILESYSTEM_READ_HARD_LIMIT = _TOOL_DEFAULTS.filesystem_read_hard_limit_bytes
DEFAULT_DIRECTORY_ENTRY_HARD_LIMIT = _TOOL_DEFAULTS.directory_entry_hard_limit

class TestFilesystemDirectoryTool:

    def setup_method(self) -> None:
        self.runtime = Runtime.open('local')
        self.human_output: list[str] = []
        self.runtime.substrate.human.output_sink = self.human_output.append

    def teardown_method(self) -> None:
        self.runtime.close()

    def test_read_write_and_delete_directory_and_file_tools(self) -> None:
        base = f'agent_outputs/fs_ops_{uuid4().hex}'
        existing_file = self._write_fixture(f'{base}/existing.txt', 'existing')
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='filesystem ops')
        self.runtime.filesystem.grant_path_list(pid, read_dirs=[base], write_dirs=[base], delete_dirs=[base], issued_by='test')
        listed = self.runtime.tools.call(pid, 'read_directory', {'path': base})
        made_dir = self.runtime.tools.call(pid, 'write_directory', {'path': f'{base}/created/nested'})
        wrote_file = self.runtime.tools.call(pid, 'write_text_file', {'path': f'{base}/created/nested/out.txt', 'content': 'created'})
        deleted_file = self.runtime.tools.call(pid, 'delete_file', {'path': existing_file})
        deleted_dir = self.runtime.tools.call(pid, 'delete_directory', {'path': f'{base}/created', 'recursive': True})
        assert listed.ok, listed.error
        assert [entry['name'] for entry in listed.payload['entries']] == ['existing.txt']
        assert made_dir.ok, made_dir.error
        assert made_dir.payload['created']
        assert wrote_file.ok, wrote_file.error
        assert deleted_file.ok, deleted_file.error
        assert not (self.runtime.workspace_root / existing_file).exists()
        assert deleted_dir.ok, deleted_dir.error
        assert not (self.runtime.workspace_root / base / 'created').exists()

    def test_delete_requires_delete_capability_not_write_capability(self) -> None:
        path = self._write_fixture(f'agent_outputs/delete_denied_{uuid4().hex}.txt', 'keep')
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='delete denied')
        self.runtime.filesystem.grant_path(pid, path, [CapabilityRight.WRITE], issued_by='test')
        denied = self.runtime.tools.call(pid, 'delete_file', {'path': path})
        assert not denied.ok
        assert 'lacks delete' in (denied.error or '')
        assert (self.runtime.workspace_root / path).exists()

    def test_delete_ask_each_time_uses_filesystem_primitive_context(self) -> None:
        path = self._write_fixture(f'agent_outputs/delete_prompt_{uuid4().hex}.txt', 'delete me')
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='delete with prompt')
        resource = self.runtime.filesystem.resource_for_path(path)
        self.runtime.capability.set_permission_policy(subject=pid, resource=resource, rights=[CapabilityRight.DELETE], policy=CapabilityManager.ASK_EACH_TIME, issued_by='test')
        with pytest.raises(HumanApprovalRequired):
            self.runtime.tools.call(pid, 'delete_file', {'path': path})
        request = self.runtime.human.pending()[0]
        processed = self.runtime.human.drain_terminal_queue(auto_approve=True)
        retried = self.runtime.tools.call(pid, 'delete_file', {'path': path})
        assert request.payload['context']['primitive'] == 'runtime.filesystem.delete_file'
        assert request.payload['context']['operation'] == 'delete_file'
        assert request.payload['context']['right'] == 'delete'
        assert 'target' in request.payload['context']
        assert processed[0].status == HumanRequestStatus.APPROVED
        assert retried.ok, retried.error
        assert not (self.runtime.workspace_root / path).exists()

    def test_truncated_utf8_read_does_not_split_codepoint(self) -> None:
        path = self._write_fixture(f'agent_outputs/utf8_{uuid4().hex}.txt', 'éx')
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='read utf8')
        self.runtime.filesystem.grant_path(pid, path, [CapabilityRight.READ], issued_by='test')
        result = self.runtime.filesystem.read_text(pid, path, max_bytes=1)
        assert result.truncated
        assert result.bytes_read == 1
        assert result.content == ''

    def test_one_time_read_capabilities_are_consumed_after_provider_read(self) -> None:
        base = f'agent_outputs/read_once_{uuid4().hex}'
        text_path = self._write_fixture(f'{base}/text.txt', 'text')
        bytes_path = self._write_fixture(f'{base}/bytes.bin', 'bytes')
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='read once')
        text_resource = self.runtime.filesystem.resource_for_path(text_path)
        bytes_resource = self.runtime.filesystem.resource_for_path(bytes_path)
        directory_resource = self.runtime.filesystem.directory_resource_for_path(base)
        self.runtime.capability.grant_once(pid, text_resource, [CapabilityRight.READ], issued_by='test')
        self.runtime.capability.grant_once(pid, bytes_resource, [CapabilityRight.READ], issued_by='test')
        self.runtime.capability.grant_once(pid, directory_resource, [CapabilityRight.READ], issued_by='test')
        assert self.runtime.filesystem.read_text(pid, text_path).content == 'text'
        assert self.runtime.filesystem.read_bytes(pid, bytes_path).content == b'bytes'
        assert self.runtime.filesystem.read_directory(pid, base).count == 2
        assert not self.runtime.capability.check(pid, text_resource, CapabilityRight.READ)
        assert not self.runtime.capability.check(pid, bytes_resource, CapabilityRight.READ)
        assert not self.runtime.capability.check(pid, directory_resource, CapabilityRight.READ)

    def test_filesystem_primitive_enforces_read_limits_without_tool_schema(self) -> None:
        path = self._write_fixture(f'agent_outputs/read_limit_{uuid4().hex}.txt', 'content')
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='read limit')
        self.runtime.filesystem.grant_path(pid, path, [CapabilityRight.READ], issued_by='test')
        with pytest.raises(ValidationError):
            self.runtime.filesystem.read_text(pid, path, max_bytes=0)
        with pytest.raises(ValidationError):
            self.runtime.filesystem.read_text(pid, path, max_bytes=DEFAULT_FILESYSTEM_READ_HARD_LIMIT + 1)

    def test_directory_primitive_enforces_limit_without_tool_schema(self) -> None:
        base = f'agent_outputs/list_limit_{uuid4().hex}'
        self._write_fixture(f'{base}/item.txt', 'content')
        pid = self.runtime.process.spawn(image='review-agent:v0', goal='directory limit')
        self.runtime.filesystem.grant_directory(pid, base, [CapabilityRight.READ], issued_by='test')
        with pytest.raises(ValidationError):
            self.runtime.filesystem.read_directory(pid, base, limit=0)
        with pytest.raises(ValidationError):
            self.runtime.filesystem.read_directory(pid, base, limit=DEFAULT_DIRECTORY_ENTRY_HARD_LIMIT + 1)

    def _write_fixture(self, path: str, content: str) -> str:
        target = self.runtime.workspace_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        return path

from __future__ import annotations
import pytest
import time
from datetime import datetime
from agent_libos import Runtime

class TestClockTool:

    def setup_method(self) -> None:
        self.runtime = Runtime.open('local')

    def teardown_method(self) -> None:
        self.runtime.close()

    def test_current_time_tool_uses_clock_primitive(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='check time')
        result = self.runtime.tools.call(pid, 'get_current_time', {'timezone': 'UTC'})
        assert result.ok, result.error
        assert result.payload['timezone'] == 'UTC'
        parsed = datetime.fromisoformat(result.payload['iso8601'])
        assert parsed.tzinfo is not None
        assert 'primitive.clock.now' in self._audit_actions()
        assert any((event.target == 'clock:now' and event.payload.get('operation') == 'now' for event in self.runtime.events.list()))

    def test_current_time_tool_supports_iana_timezone(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='check shanghai time')
        result = self.runtime.tools.call(pid, 'get_current_time', {'timezone': 'Asia/Shanghai'})
        assert result.ok, result.error
        assert result.payload['timezone'] == 'Asia/Shanghai'
        assert '+08:00' in result.payload['iso8601']

    def test_sleep_tool_uses_clock_primitive(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='sleep briefly')
        started = time.monotonic()
        result = self.runtime.tools.call(pid, 'sleep', {'seconds': 0.02})
        elapsed = time.monotonic() - started
        assert result.ok, result.error
        assert result.payload['requested_seconds'] == 0.02
        assert result.payload['elapsed_seconds'] >= 0.0
        assert elapsed >= 0.015
        assert 'primitive.clock.sleep' in self._audit_actions()
        assert any((event.target == 'clock:sleep' and event.payload.get('operation') == 'sleep' for event in self.runtime.events.list()))

    def test_sleep_tool_rejects_unbounded_duration(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='sleep too long')
        result = self.runtime.tools.call(pid, 'sleep', {'seconds': 61})
        assert not result.ok
        assert 'Invalid arguments' in (result.error or '')
        assert 'primitive.clock.sleep' not in self._audit_actions()

    def test_clock_tools_are_in_process_tool_table(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='time tools')
        names = {schema['function']['name'] for schema in self.runtime.tools.openai_tool_schemas(pid)}
        assert 'get_current_time' in names
        assert 'sleep' in names

    def _audit_actions(self) -> list[str]:
        return [record.action for record in self.runtime.audit.trace()]

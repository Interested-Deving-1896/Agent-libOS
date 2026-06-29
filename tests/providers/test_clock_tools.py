from __future__ import annotations
import asyncio
import pytest
import time
from datetime import datetime
from agent_libos import Runtime
from agent_libos.models import CapabilityRight, ExternalEffectClassification, ExternalEffectRollbackClass, ExternalEffectRollbackStatus
from agent_libos.models.exceptions import CapabilityDenied, ValidationError

class TestClockTool:

    def setup_method(self) -> None:
        self.runtime = Runtime.open('local')

    def teardown_method(self) -> None:
        self.runtime.close()

    def test_current_time_tool_uses_clock_primitive(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='check time')
        self.runtime.capability.grant(pid, 'clock:now', [CapabilityRight.READ], issued_by='test')
        result = self.runtime.tools.call(pid, 'get_current_time', {'timezone': 'UTC'})
        assert result.ok, result.error
        assert result.payload['timezone'] == 'UTC'
        parsed = datetime.fromisoformat(result.payload['iso8601'])
        assert parsed.tzinfo is not None
        assert 'primitive.clock.now' in self._audit_actions()
        assert any((event.target == 'clock:now' and event.payload.get('operation') == 'now' for event in self.runtime.events.list()))

    def test_current_time_tool_supports_iana_timezone(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='check shanghai time')
        self.runtime.capability.grant(pid, 'clock:*', [CapabilityRight.READ], issued_by='test')
        result = self.runtime.tools.call(pid, 'get_current_time', {'timezone': 'Asia/Shanghai'})
        assert result.ok, result.error
        assert result.payload['timezone'] == 'Asia/Shanghai'
        assert '+08:00' in result.payload['iso8601']

    def test_sleep_tool_uses_clock_primitive(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='sleep briefly')
        self.runtime.capability.grant(pid, 'clock:sleep', [CapabilityRight.READ], issued_by='test')
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

    def test_clock_primitive_rejects_non_finite_sleep_duration(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='sleep nan')
        with pytest.raises(ValidationError, match='finite'):
            self.runtime.clock.sleep(pid, float('nan'))
        assert 'primitive.clock.sleep' not in self._audit_actions()

    def test_clock_now_requires_capability_before_provider_read(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='clock now denied')
        provider = RecordingClockProvider()
        self.runtime.clock.provider = provider

        with pytest.raises(CapabilityDenied, match='clock:now'):
            self.runtime.clock.now(pid, tz='UTC')

        assert provider.now_calls == 0
        assert 'primitive.clock.now' not in self._audit_actions()
        assert not any(event.target == 'clock:now' for event in self.runtime.events.list())

    def test_clock_sleep_requires_capability_before_provider_wait(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='clock sleep denied')
        provider = RecordingClockProvider()
        self.runtime.clock.provider = provider

        with pytest.raises(CapabilityDenied, match='clock:sleep'):
            self.runtime.clock.sleep(pid, 0.0)
        with pytest.raises(CapabilityDenied, match='clock:sleep'):
            asyncio.run(self.runtime.clock.asleep(pid, 0.0))

        assert provider.monotonic_calls == 0
        assert provider.sleep_calls == []
        assert provider.asleep_calls == []
        assert 'primitive.clock.sleep' not in self._audit_actions()
        assert not any(event.target == 'clock:sleep' for event in self.runtime.events.list())

    def test_clock_tools_cannot_bypass_missing_primitive_capability(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='clock tools denied')

        now = self.runtime.tools.call(pid, 'get_current_time', {'timezone': 'UTC'})
        slept = self.runtime.tools.call(pid, 'sleep', {'seconds': 0.0})

        assert not now.ok
        assert not slept.ok
        assert now.payload['error']['code'] == 'permission_denied'
        assert slept.payload['error']['code'] == 'permission_denied'
        assert 'clock:now' in (now.error or '')
        assert 'clock:sleep' in (slept.error or '')
        assert 'primitive.clock.now' not in self._audit_actions()
        assert 'primitive.clock.sleep' not in self._audit_actions()

    def test_clock_one_time_capabilities_are_consumed_after_success(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='clock one-shot')
        self.runtime.capability.grant_once(pid, 'clock:now', [CapabilityRight.READ], issued_by='test')
        self.runtime.capability.grant_once(pid, 'clock:sleep', [CapabilityRight.READ], issued_by='test')

        now = self.runtime.clock.now(pid, tz='UTC')
        slept = self.runtime.clock.sleep(pid, 0.0)

        assert now.timezone == 'UTC'
        assert slept.requested_seconds == 0.0
        with pytest.raises(CapabilityDenied, match='clock:now'):
            self.runtime.clock.now(pid, tz='UTC')
        with pytest.raises(CapabilityDenied, match='clock:sleep'):
            self.runtime.clock.sleep(pid, 0.0)

    def test_clock_one_time_now_is_restored_when_provider_fails_before_read(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='clock failure restore')
        cap = self.runtime.capability.grant_once(pid, 'clock:now', [CapabilityRight.READ], issued_by='test')
        provider = RecordingClockProvider(fail_now=True)
        self.runtime.clock.provider = provider

        with pytest.raises(RuntimeError, match='clock provider failed'):
            self.runtime.clock.now(pid, tz='UTC')

        restored = self.runtime.store.get_capability(cap.cap_id)
        assert restored is not None
        assert restored.uses_remaining == 1
        assert self.runtime.capability.check(pid, 'clock:now', CapabilityRight.READ)
        assert 'primitive.clock.now' not in self._audit_actions()

    def test_clock_tools_are_in_process_tool_table(self) -> None:
        pid = self.runtime.process.spawn(image='base-agent:v0', goal='time tools')
        names = {schema['function']['name'] for schema in self.runtime.tools.openai_tool_schemas(pid)}
        assert 'get_current_time' in names
        assert 'sleep' in names

    def _audit_actions(self) -> list[str]:
        return [record.action for record in self.runtime.audit.trace()]


class RecordingClockProvider:
    def __init__(self, *, fail_now: bool = False) -> None:
        self.fail_now = fail_now
        self.now_calls = 0
        self.monotonic_calls = 0
        self.sleep_calls: list[float] = []
        self.asleep_calls: list[float] = []

    def now(self, timezone_):
        self.now_calls += 1
        if self.fail_now:
            raise RuntimeError('clock provider failed')
        return datetime(2040, 1, 2, 3, 4, 5, tzinfo=timezone_)

    def monotonic(self) -> float:
        self.monotonic_calls += 1
        return float(self.monotonic_calls)

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)

    async def asleep(self, seconds: float) -> None:
        self.asleep_calls.append(seconds)

    def classify_external_effect(self, operation: str, context: dict, result) -> ExternalEffectClassification:
        return ExternalEffectClassification(
            rollback_class=ExternalEffectRollbackClass.NO_ROLLBACK_REQUIRED,
            rollback_status=ExternalEffectRollbackStatus.NOT_REQUIRED,
            state_mutation=False,
            information_flow=True,
            metadata={'operation': operation},
        )

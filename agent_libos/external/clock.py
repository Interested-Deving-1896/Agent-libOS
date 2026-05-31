from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_libos.exceptions import ValidationError
from agent_libos.models import EventType
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus


@dataclass(frozen=True)
class ClockNowResult:
    iso8601: str
    unix_seconds: float
    timezone: str


@dataclass(frozen=True)
class SleepResult:
    requested_seconds: float
    elapsed_seconds: float


class ClockPrimitive:
    """Clock primitive used by scheduler-facing tools."""

    FIXED_TIMEZONE_FALLBACKS = {
        "Asia/Shanghai": timezone(timedelta(hours=8), name="Asia/Shanghai"),
    }

    def __init__(self, audit: AuditManager, events: EventBus, max_sleep_seconds: float = 60.0):
        self.audit = audit
        self.events = events
        self.max_sleep_seconds = max_sleep_seconds

    def now(self, pid: str, tz: str = "UTC") -> ClockNowResult:
        selected_tz = self._timezone(tz)
        current = datetime.now(selected_tz)
        result = ClockNowResult(
            iso8601=current.isoformat(),
            unix_seconds=current.timestamp(),
            timezone=tz,
        )
        self.events.emit(
            EventType.EXTERNAL_READ,
            source=pid,
            target="clock:now",
            payload={"adapter": "clock", "operation": "now", "timezone": tz, "iso8601": result.iso8601},
        )
        self.audit.record(
            actor=pid,
            action="external.clock.now",
            target="clock:now",
            decision={"timezone": tz, "iso8601": result.iso8601},
        )
        return result

    def sleep(self, pid: str, seconds: float) -> SleepResult:
        duration = self._validate_sleep_duration(seconds)
        started = time.monotonic()
        time.sleep(duration)
        elapsed = time.monotonic() - started
        return self._record_sleep(pid, duration, elapsed)

    async def asleep(self, pid: str, seconds: float) -> SleepResult:
        duration = self._validate_sleep_duration(seconds)
        started = time.monotonic()
        # Use asyncio sleep so one sleeping AgentProcess does not block other
        # runnable process tasks in the scheduler.
        await asyncio.sleep(duration)
        elapsed = time.monotonic() - started
        return self._record_sleep(pid, duration, elapsed)

    def _validate_sleep_duration(self, seconds: float) -> float:
        duration = float(seconds)
        if duration < 0:
            raise ValidationError("sleep seconds must be non-negative")
        if duration > self.max_sleep_seconds:
            raise ValidationError(f"sleep seconds exceeds max_sleep_seconds={self.max_sleep_seconds}")
        return duration

    def _record_sleep(self, pid: str, duration: float, elapsed: float) -> SleepResult:
        result = SleepResult(requested_seconds=duration, elapsed_seconds=elapsed)
        self.events.emit(
            EventType.EXTERNAL_READ,
            source=pid,
            target="clock:sleep",
            payload={
                "adapter": "clock",
                "operation": "sleep",
                "requested_seconds": duration,
                "elapsed_seconds": elapsed,
            },
        )
        self.audit.record(
            actor=pid,
            action="external.clock.sleep",
            target="clock:sleep",
            decision={"requested_seconds": duration, "elapsed_seconds": elapsed},
        )
        return result

    def _timezone(self, tz: str):
        if tz.upper() == "UTC":
            return timezone.utc
        try:
            return ZoneInfo(tz)
        except ZoneInfoNotFoundError as exc:
            if tz in self.FIXED_TIMEZONE_FALLBACKS:
                return self.FIXED_TIMEZONE_FALLBACKS[tz]
            raise ValidationError(f"unknown timezone: {tz}") from exc

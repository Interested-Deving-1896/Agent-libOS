from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.models.exceptions import ValidationError
from agent_libos.models import CapabilityDecision, CapabilityRight, EventType
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.runtime.external_effects import (
    classify_external_effect,
    record_external_effect,
    require_external_effect_classifier,
)
from agent_libos.substrate import ClockProvider, LocalClockProvider

_TOOL_DEFAULTS = DEFAULT_CONFIG.tools
_CLOCK_NOW_RESOURCE = "clock:now"
_CLOCK_SLEEP_RESOURCE = "clock:sleep"


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

    def __init__(
        self,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus,
        max_sleep_seconds: float = _TOOL_DEFAULTS.max_sleep_seconds,
        provider: ClockProvider | None = None,
    ):
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        self.max_sleep_seconds = max_sleep_seconds
        self.provider = provider or LocalClockProvider()

    def now(self, pid: str, tz: str = _TOOL_DEFAULTS.clock_timezone) -> ClockNowResult:
        selected_tz = self._timezone(tz)
        resource = _CLOCK_NOW_RESOURCE
        operation_context = self._authorization_context(
            pid=pid,
            resource=resource,
            primitive="runtime.clock.now",
            operation="now",
            extra={"timezone": tz},
        )
        decision = self.capabilities.require(pid, resource, CapabilityRight.READ, operation_context)
        effect_context = {"timezone": tz, "resource": resource}
        require_external_effect_classifier(self.provider, "now")
        self._claim_one_time_decision(decision, operation="now")
        try:
            current = self.provider.now(selected_tz)
        except Exception:
            self._restore_one_time_decision(decision, operation="now")
            raise
        result = ClockNowResult(
            iso8601=current.isoformat(),
            unix_seconds=current.timestamp(),
            timezone=tz,
        )
        event = self.events.emit(
            EventType.EXTERNAL_READ,
            source=pid,
            target=resource,
            payload={"adapter": "clock", "operation": "now", "timezone": tz, "iso8601": result.iso8601},
        )
        audit_record = self.audit.record(
            actor=pid,
            action="primitive.clock.now",
            target=resource,
            decision={"timezone": tz, "iso8601": result.iso8601},
        )
        classification = classify_external_effect(self.provider, "now", effect_context, result.__dict__)
        record_external_effect(
            self.audit.store,
            pid=pid,
            provider="clock",
            operation="now",
            target=resource,
            classification=classification,
            audit_record=audit_record,
            event=event,
            metadata={"context": effect_context, "result": result.__dict__},
        )
        return result

    def sleep(self, pid: str, seconds: float) -> SleepResult:
        duration = self._validate_sleep_duration(seconds)
        decision = self._authorize_sleep(pid, duration)
        require_external_effect_classifier(self.provider, "sleep")
        self._claim_one_time_decision(decision, operation="sleep")
        committed = False
        try:
            started = self.provider.monotonic()
            self.provider.sleep(duration)
            committed = True
        except Exception:
            if not committed:
                self._restore_one_time_decision(decision, operation="sleep")
            raise
        elapsed = self.provider.monotonic() - started
        return self._record_sleep(pid, duration, elapsed)

    async def asleep(self, pid: str, seconds: float) -> SleepResult:
        duration = self._validate_sleep_duration(seconds)
        decision = self._authorize_sleep(pid, duration)
        require_external_effect_classifier(self.provider, "sleep")
        self._claim_one_time_decision(decision, operation="sleep")
        committed = False
        try:
            started = self.provider.monotonic()
            await self.provider.asleep(duration)
            committed = True
        except Exception:
            if not committed:
                self._restore_one_time_decision(decision, operation="sleep")
            raise
        elapsed = self.provider.monotonic() - started
        return self._record_sleep(pid, duration, elapsed)

    def _validate_sleep_duration(self, seconds: float) -> float:
        duration = float(seconds)
        if not math.isfinite(duration):
            raise ValidationError("sleep seconds must be finite")
        if duration < 0:
            raise ValidationError("sleep seconds must be non-negative")
        if duration > self.max_sleep_seconds:
            raise ValidationError(f"sleep seconds exceeds max_sleep_seconds={self.max_sleep_seconds}")
        return duration

    def _record_sleep(self, pid: str, duration: float, elapsed: float) -> SleepResult:
        result = SleepResult(requested_seconds=duration, elapsed_seconds=elapsed)
        effect_context = {"requested_seconds": duration, "resource": _CLOCK_SLEEP_RESOURCE}
        event = self.events.emit(
            EventType.EXTERNAL_READ,
            source=pid,
            target=_CLOCK_SLEEP_RESOURCE,
            payload={
                "adapter": "clock",
                "operation": "sleep",
                "requested_seconds": duration,
                "elapsed_seconds": elapsed,
            },
        )
        audit_record = self.audit.record(
            actor=pid,
            action="primitive.clock.sleep",
            target=_CLOCK_SLEEP_RESOURCE,
            decision={"requested_seconds": duration, "elapsed_seconds": elapsed},
        )
        classification = classify_external_effect(self.provider, "sleep", effect_context, result.__dict__)
        record_external_effect(
            self.audit.store,
            pid=pid,
            provider="clock",
            operation="sleep",
            target=_CLOCK_SLEEP_RESOURCE,
            classification=classification,
            audit_record=audit_record,
            event=event,
            metadata={"context": effect_context, "result": result.__dict__},
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

    def _authorize_sleep(self, pid: str, duration: float) -> CapabilityDecision:
        return self.capabilities.require(
            pid,
            _CLOCK_SLEEP_RESOURCE,
            CapabilityRight.READ,
            self._authorization_context(
                pid=pid,
                resource=_CLOCK_SLEEP_RESOURCE,
                primitive="runtime.clock.sleep",
                operation="sleep",
                extra={"requested_seconds": duration},
            ),
        )

    def _authorization_context(
        self,
        *,
        pid: str,
        resource: str,
        primitive: str,
        operation: str,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "pid": pid,
            "primitive": primitive,
            "operation": operation,
            "resource": resource,
            "right": CapabilityRight.READ.value,
            **extra,
        }

    def _claim_one_time_decision(self, decision: CapabilityDecision, *, operation: str) -> None:
        self.capabilities.claim_decision_use(
            decision,
            used_by="clock",
            reason=f"one-time clock {operation} permission reserved",
        )

    def _restore_one_time_decision(self, decision: CapabilityDecision, *, operation: str) -> None:
        if decision.consume_capability_id is None:
            return
        self.capabilities._restore_reserved_use(
            decision.consume_capability_id,
            restored_by="clock",
            reason=f"one-time clock {operation} permission restored after provider failure",
        )

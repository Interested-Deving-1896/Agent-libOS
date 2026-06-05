from __future__ import annotations

import os
import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig, ShellCommandRule, ShellPolicyLevel
from agent_libos.models import Capability, CapabilityRight, EventType
from agent_libos.models.exceptions import CapabilityDenied, HumanApprovalRequired, ValidationError
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.substrate import CommandResult, LocalShellProvider, ShellProvider
from agent_libos.utils.ids import utc_now

_TOOL_DEFAULTS = DEFAULT_CONFIG.tools

_WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")


@dataclass(frozen=True)
class ShellPolicyDecision:
    allowed: bool
    ask_human: bool
    reason: str
    policy_level: str | None
    matched_rule: tuple[str, ...] | None = None
    high_risk: bool = False
    consume_once: bool = False


class ShellAdapter:
    """Capability-checked shell primitive.

    Commands are accepted only as argv arrays and are executed by the substrate
    with shell=False. Allow/ask decisions use exact token rules; no glob,
    substring, or shell-style parsing is used for whitelist matching.
    """

    def __init__(
        self,
        capabilities: CapabilityManager,
        audit: AuditManager,
        events: EventBus | None = None,
        cwd: str | os.PathLike[str] | None = None,
        human: Any | None = None,
        provider: ShellProvider | None = None,
        config: AgentLibOSConfig | None = None,
    ):
        self.config = config or DEFAULT_CONFIG
        self.capabilities = capabilities
        self.audit = audit
        self.events = events
        self.human = human
        self.provider = provider or LocalShellProvider(cwd or ".")

    def run(
        self,
        pid: str,
        argv: list[str],
        timeout: float = _TOOL_DEFAULTS.shell_timeout_s,
        cwd: str | os.PathLike[str] | None = None,
    ) -> CommandResult:
        checked = self._validate_argv(argv)
        selected_timeout = self._validate_timeout(timeout)
        resource = self.resource_for(checked)
        decision = self._authorize(pid, checked, resource, timeout=selected_timeout)
        if decision.ask_human:
            self._request_human_approval(pid, checked, resource, decision, timeout=selected_timeout, cwd=cwd)
        if not decision.allowed:
            raise CapabilityDenied(f"{pid} denied shell execute on {resource}: {decision.reason}")
        try:
            if cwd is None:
                proc = self.provider.run(checked, timeout=selected_timeout)
            else:
                proc = self.provider.run(checked, timeout=selected_timeout, cwd=os.fspath(cwd))
            proc = self._bounded_result(proc)
        except subprocess.TimeoutExpired as exc:
            self.audit.record(
                actor=pid,
                action="primitive.shell.timeout",
                target=resource,
                decision={"argv": checked, "timeout_s": selected_timeout, "cwd": os.fspath(cwd) if cwd is not None else None},
            )
            raise TimeoutError(f"shell command timed out after {selected_timeout}s: {checked}") from exc
        finally:
            if decision.consume_once:
                self.capabilities.consume_allow_once(
                    subject=pid,
                    resource=resource,
                    right=CapabilityRight.EXECUTE,
                    used_by="shell",
                )
        self._emit_run_event(pid, resource, checked, proc, decision, cwd=cwd)
        self.audit.record(
            actor=pid,
            action="primitive.shell.run",
            target=resource,
            decision={
                "argv": checked,
                "returncode": proc.returncode,
                "policy_level": decision.policy_level,
                "policy_reason": decision.reason,
                "matched_rule": list(decision.matched_rule) if decision.matched_rule else None,
                "high_risk": decision.high_risk,
                "cwd": os.fspath(cwd) if cwd is not None else None,
                "stdout_truncated": proc.stdout_truncated,
                "stderr_truncated": proc.stderr_truncated,
            },
        )
        return proc

    async def arun(
        self,
        pid: str,
        argv: list[str],
        timeout: float = _TOOL_DEFAULTS.shell_timeout_s,
        cwd: str | os.PathLike[str] | None = None,
    ) -> CommandResult:
        return await asyncio.to_thread(self.run, pid, argv, timeout=timeout, cwd=cwd)

    def grant_policy(
        self,
        pid: str,
        level: ShellPolicyLevel | str | None = None,
        *,
        issued_by: str = "shell",
    ) -> Capability:
        selected = self._normalize_policy_level(level or self.config.shell.default_policy_level)
        return self.capabilities.grant(
            subject=pid,
            resource=self.policy_resource(),
            rights=[CapabilityRight.EXECUTE],
            issued_by=issued_by,
            constraints={self.config.shell.policy_capability_key: selected},
        )

    def policy_resource(self) -> str:
        return self.config.shell.policy_resource

    def resource_for(self, argv: list[str]) -> str:
        command = self._command_identity(argv[0])
        return f"shell:{command}"

    def _authorize(self, pid: str, argv: list[str], resource: str, *, timeout: float) -> ShellPolicyDecision:
        policy_caps = self._matching_shell_policy_caps(pid, resource)
        if any(
            cap.constraints.get(self.config.shell.policy_capability_key) == self.config.shell.always_deny_level
            for cap in policy_caps
        ):
            return ShellPolicyDecision(
                allowed=False,
                ask_human=False,
                reason="shell policy is always_deny",
                policy_level=self.config.shell.always_deny_level,
            )

        legacy_policy, legacy_consume_once = self._legacy_permission_policy(pid, resource)
        if legacy_policy == CapabilityManager.ALWAYS_DENY:
            return ShellPolicyDecision(False, False, "permission policy denied command", legacy_policy)
        if legacy_policy == CapabilityManager.ASK_EACH_TIME:
            return ShellPolicyDecision(False, True, "permission policy requires approval", legacy_policy)
        if legacy_policy in {CapabilityManager.ALWAYS_ALLOW, CapabilityManager.ALLOW_ONCE}:
            return ShellPolicyDecision(
                allowed=True,
                ask_human=False,
                reason="explicit command capability allowed command",
                policy_level=legacy_policy,
                consume_once=legacy_consume_once,
            )

        if not policy_caps:
            raise CapabilityDenied(f"{pid} lacks shell execute policy for {resource}")

        level = self._selected_policy_level(policy_caps)
        whitelist_match = self._first_matching_rule(argv, self.config.shell.whitelist, allow_bare_only=True)
        blacklist_match = self._first_matching_blacklist_rule(argv)

        if level == self.config.shell.allowlist_auto_else_ask_level:
            if whitelist_match is not None:
                return ShellPolicyDecision(
                    allowed=True,
                    ask_human=False,
                    reason="command matched shell whitelist",
                    policy_level=level,
                    matched_rule=whitelist_match.argv,
                )
            return ShellPolicyDecision(
                allowed=False,
                ask_human=True,
                reason="command did not match shell whitelist",
                policy_level=level,
            )
        if level == self.config.shell.blocklist_ask_else_auto_level:
            if blacklist_match is not None:
                return ShellPolicyDecision(
                    allowed=False,
                    ask_human=True,
                    reason="command matched shell blacklist",
                    policy_level=level,
                    matched_rule=blacklist_match.argv,
                )
            return ShellPolicyDecision(
                allowed=True,
                ask_human=False,
                reason="command did not match shell blacklist",
                policy_level=level,
            )
        if level == self.config.shell.always_allow_level:
            return ShellPolicyDecision(
                allowed=True,
                ask_human=False,
                reason="shell policy is always_allow",
                policy_level=level,
                high_risk=True,
            )
        return ShellPolicyDecision(False, False, f"unsupported shell policy level: {level}", level)

    def _request_human_approval(
        self,
        pid: str,
        argv: list[str],
        resource: str,
        decision: ShellPolicyDecision,
        *,
        timeout: float,
        cwd: str | os.PathLike[str] | None,
    ) -> None:
        if self.human is None:
            raise CapabilityDenied(f"{pid} requires human approval for shell execute on {resource}")
        request_id = self.human.query(
            pid=pid,
            human=self.config.runtime.default_human,
            request={
                "type": "external_operation_approval",
                "question": f"Allow this process to run shell command {argv[0]!r}?",
                "requested_once_capability": {
                    "subject": pid,
                    "resource": resource,
                    "rights": [CapabilityRight.EXECUTE.value],
                },
                "context": {
                    "adapter": "shell",
                    "primitive": "runtime.shell.run",
                    "operation": "run",
                    "pid": pid,
                    "workspace_root": str(getattr(self.provider, "cwd", "")),
                    "working_directory": os.fspath(cwd) if cwd is not None else ".",
                    "argv": list(argv),
                    "command": argv[0],
                    "resource": resource,
                    "right": CapabilityRight.EXECUTE.value,
                    "grant_scope": "one_time",
                    "timeout_s": timeout,
                    "policy_level": decision.policy_level,
                    "policy_reason": decision.reason,
                    "matched_rule": list(decision.matched_rule) if decision.matched_rule else None,
                    "high_risk": decision.high_risk,
                },
            },
            blocking=True,
        )
        raise HumanApprovalRequired(
            request_id=request_id,
            message=f"{pid} is waiting for per-use human approval to run {resource}",
        )

    def _emit_run_event(
        self,
        pid: str,
        resource: str,
        argv: list[str],
        proc: CommandResult,
        decision: ShellPolicyDecision,
        *,
        cwd: str | os.PathLike[str] | None,
    ) -> None:
        if self.events is None:
            return
        self.events.emit(
            EventType.EXTERNAL_WRITE,
            source=pid,
            target=resource,
            payload={
                "adapter": "shell",
                "operation": "run",
                "argv": argv,
                "returncode": proc.returncode,
                "policy_level": decision.policy_level,
                "high_risk": decision.high_risk,
                "cwd": os.fspath(cwd) if cwd is not None else None,
            },
        )

    def _matching_shell_policy_caps(self, pid: str, resource: str) -> list[Capability]:
        caps = [
            cap
            for cap in self._matching_shell_caps(pid, resource)
            if self.config.shell.policy_capability_key in cap.constraints
        ]
        caps.sort(key=lambda cap: (len(cap.resource), cap.issued_at), reverse=True)
        return caps

    def _legacy_permission_policy(self, pid: str, resource: str) -> tuple[str, bool]:
        caps = [
            cap
            for cap in self._matching_shell_caps(pid, resource)
            if self.config.shell.policy_capability_key not in cap.constraints
        ]
        if not caps:
            return CapabilityManager.MISSING, False
        caps.sort(key=lambda cap: (len(cap.resource), cap.issued_at), reverse=True)
        cap = caps[0]
        policy = str(cap.constraints.get(CapabilityManager.POLICY_KEY) or CapabilityManager.ALWAYS_ALLOW)
        return policy, policy == CapabilityManager.ALLOW_ONCE

    def _matching_shell_caps(self, pid: str, resource: str) -> list[Capability]:
        now = utc_now()
        result: list[Capability] = []
        for cap in self.capabilities.capabilities_for(pid):
            if cap.revoked:
                continue
            if cap.expires_at is not None and cap.expires_at <= now:
                continue
            if CapabilityRight.EXECUTE.value not in cap.rights and "*" not in cap.rights:
                continue
            if not self._resource_matches(cap.resource, resource):
                continue
            result.append(cap)
        return result

    def _selected_policy_level(self, caps: list[Capability]) -> str:
        return self._normalize_policy_level(caps[0].constraints[self.config.shell.policy_capability_key])

    def _normalize_policy_level(self, value: Any) -> str:
        normalized = str(value).strip().lower()
        allowed = {
            self.config.shell.always_deny_level,
            self.config.shell.allowlist_auto_else_ask_level,
            self.config.shell.blocklist_ask_else_auto_level,
            self.config.shell.always_allow_level,
        }
        if normalized not in allowed:
            raise ValidationError(f"unknown shell policy level: {value!r}")
        return normalized

    def _first_matching_blacklist_rule(self, argv: list[str]) -> ShellCommandRule | None:
        direct = self._first_matching_rule(argv, self.config.shell.blacklist, allow_bare_only=False)
        if direct is not None:
            return direct
        # Some executables such as env/nohup/sudo can dispatch another program.
        # In blacklist mode, scan later tokens for exact executable identities
        # so `env bash -c ...` still requires human approval.
        executable_tokens = {self._normalize_executable(rule.argv[0]) for rule in self.config.shell.blacklist}
        for token in argv[1:]:
            if self._normalize_executable(token) in executable_tokens:
                return ShellCommandRule((token,), match="exact", description="nested blacklist executable")
        return None

    def _first_matching_rule(
        self,
        argv: list[str],
        rules: tuple[ShellCommandRule, ...],
        *,
        allow_bare_only: bool,
    ) -> ShellCommandRule | None:
        return next((rule for rule in rules if self._rule_matches(argv, rule, allow_bare_only=allow_bare_only)), None)

    def _rule_matches(self, argv: list[str], rule: ShellCommandRule, *, allow_bare_only: bool) -> bool:
        if not rule.argv:
            return False
        if allow_bare_only and self._argv0_has_path(argv[0]) and not self._argv0_has_path(rule.argv[0]):
            return False
        if rule.match == "exact" and len(argv) != len(rule.argv):
            return False
        if len(argv) < len(rule.argv):
            return False
        for index, expected in enumerate(rule.argv):
            actual = argv[index]
            if index == 0:
                if self._normalize_executable(actual) != self._normalize_executable(expected):
                    return False
                continue
            if actual != expected:
                return False
        return True

    def _validate_argv(self, argv: list[str]) -> list[str]:
        if not isinstance(argv, list) or not argv:
            raise ValidationError("shell argv must be a non-empty list")
        checked: list[str] = []
        for index, value in enumerate(argv):
            if not isinstance(value, str):
                raise ValidationError(f"shell argv[{index}] must be a string")
            if "\x00" in value:
                raise ValidationError(f"shell argv[{index}] cannot contain NUL bytes")
            if index == 0 and not value.strip():
                raise ValidationError("shell argv[0] must be non-empty")
            checked.append(value)
        return checked

    def _validate_timeout(self, timeout: float) -> float:
        try:
            selected = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValidationError("shell timeout must be a number") from exc
        if selected <= 0:
            raise ValidationError("shell timeout must be > 0")
        if selected > self.config.shell.timeout_hard_limit_s:
            raise ValidationError(f"shell timeout exceeds hard limit {self.config.shell.timeout_hard_limit_s}s")
        return selected

    def _bounded_result(self, proc: CommandResult) -> CommandResult:
        stdout, stdout_truncated = self._truncate_output(proc.stdout, self.config.shell.max_stdout_chars)
        stderr, stderr_truncated = self._truncate_output(proc.stderr, self.config.shell.max_stderr_chars)
        return CommandResult(
            argv=proc.argv,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=proc.stdout_truncated or stdout_truncated,
            stderr_truncated=proc.stderr_truncated or stderr_truncated,
        )

    def _truncate_output(self, value: str, limit: int) -> tuple[str, bool]:
        if len(value) <= limit:
            return value, False
        return value[:limit], True

    def _command_identity(self, argv0: str) -> str:
        return self._normalize_executable(argv0)

    def _normalize_executable(self, value: str) -> str:
        raw = value.strip().replace("\\", "/")
        name = PurePath(raw).name or raw
        lowered = name.casefold()
        for suffix in _WINDOWS_EXECUTABLE_SUFFIXES:
            if lowered.endswith(suffix):
                return lowered[: -len(suffix)]
        return lowered

    def _argv0_has_path(self, value: str) -> bool:
        return "/" in value or "\\" in value or PurePath(value).is_absolute()

    def _resource_matches(self, granted: str, requested: str) -> bool:
        if granted == "*" or granted == requested:
            return True
        if granted.endswith(":*"):
            return requested.startswith(granted[:-1])
        return False

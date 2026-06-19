from __future__ import annotations

import os
import asyncio
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.capability.rules import AUTHORITY_RULES_KEY, ShellRuleEngine
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig, ShellCommandRule, ShellPolicyLevel
from agent_libos.models import AuthorityRisk, AuthorityRule, Capability, CapabilityEffect, CapabilityRight, EventType, SandboxProfile
from agent_libos.models.exceptions import CapabilityDenied, HumanApprovalRequired, ValidationError
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.runtime.external_effects import (
    classify_external_effect,
    record_external_effect,
    require_external_effect_classifier,
)
from agent_libos.substrate import CommandResult, LocalShellProvider, ShellProvider

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
    consume_capability_id: str | None = None
    risk: AuthorityRisk = AuthorityRisk.MEDIUM
    rule_id: str | None = None
    rule_effect: CapabilityEffect = CapabilityEffect.ASK
    sandbox_profile: SandboxProfile | None = None


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
        self.rule_engine = ShellRuleEngine(self._configured_rules())

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
        selected_cwd = os.fspath(cwd) if cwd is not None else "."
        decision = self._authorize(pid, checked, resource, timeout=selected_timeout, cwd=selected_cwd)
        if decision.ask_human:
            self._request_human_approval(pid, checked, resource, decision, timeout=selected_timeout, cwd=cwd)
        if not decision.allowed:
            raise CapabilityDenied(f"{pid} denied shell execute on {resource}: {decision.reason}")
        effect_context = {
            "argv": list(checked),
            "resource": resource,
            "timeout_s": selected_timeout,
            "cwd": os.fspath(cwd) if cwd is not None else None,
            "policy_level": decision.policy_level,
            "high_risk": decision.high_risk,
            "risk": decision.risk.value,
            "rule_id": decision.rule_id,
            "rule_effect": decision.rule_effect.value,
            "sandbox_profile": self._profile_json(decision.sandbox_profile),
        }
        require_external_effect_classifier(self.provider, "run")
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
                if decision.consume_capability_id is not None:
                    self.capabilities.consume_use(
                        decision.consume_capability_id,
                        used_by="shell",
                        reason="one-time shell permission consumed",
                    )
        event = self._emit_run_event(pid, resource, checked, proc, decision, cwd=cwd)
        audit_record = self.audit.record(
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
                "risk": decision.risk.value,
                "rule_id": decision.rule_id,
                "sandbox_profile": self._profile_json(decision.sandbox_profile),
                "cwd": os.fspath(cwd) if cwd is not None else None,
                "stdout_truncated": proc.stdout_truncated,
                "stderr_truncated": proc.stderr_truncated,
            },
        )
        classification = classify_external_effect(
            self.provider,
            "run",
            effect_context,
            {"returncode": proc.returncode, "stdout_truncated": proc.stdout_truncated, "stderr_truncated": proc.stderr_truncated},
        )
        record_external_effect(
            self.audit.store,
            pid=pid,
            provider="shell",
            operation="run",
            target=resource,
            classification=classification,
            audit_record=audit_record,
            event=event,
            metadata={"context": effect_context, "returncode": proc.returncode},
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

    def _authorize(self, pid: str, argv: list[str], resource: str, *, timeout: float, cwd: str) -> ShellPolicyDecision:
        rule_match = self.rule_engine.classify(argv)
        rule = rule_match.rule
        profile = self.capabilities.profiles.shell(
            resource=resource,
            effect=rule.effect,
            risk=rule.risk,
            rule_id=rule.rule_id,
            argv=argv,
            timeout_s=timeout,
            cwd=cwd,
        )
        operation_context = self._operation_context(pid, argv, resource, timeout=timeout, cwd=cwd, profile=profile)
        policy_caps = self._matching_shell_policy_caps(pid, resource, operation_context)
        if any(
            cap.effect == CapabilityEffect.DENY
            or
            cap.constraints.get(self.config.shell.policy_capability_key) == self.config.shell.always_deny_level
            for cap in policy_caps
        ):
            return ShellPolicyDecision(
                allowed=False,
                ask_human=False,
                reason="shell policy is always_deny",
                policy_level=self.config.shell.always_deny_level,
                matched_rule=rule_match.matched_argv,
                risk=rule.risk,
                rule_id=rule.rule_id,
                rule_effect=rule.effect,
                sandbox_profile=profile,
            )
        if rule.effect == CapabilityEffect.DENY:
            return ShellPolicyDecision(
                allowed=False,
                ask_human=False,
                reason=rule.description or "shell rule denied command",
                policy_level=None,
                matched_rule=rule_match.matched_argv,
                high_risk=True,
                risk=rule.risk,
                rule_id=rule.rule_id,
                rule_effect=rule.effect,
                sandbox_profile=profile,
            )

        explicit_decision = self._explicit_command_decision(pid, resource, operation_context)
        explicit_policy = explicit_decision.policy
        if explicit_policy == CapabilityManager.ALWAYS_DENY:
            return ShellPolicyDecision(
                False,
                False,
                "explicit command capability denied command",
                explicit_policy,
                matched_rule=rule_match.matched_argv,
                high_risk=self._is_high_risk(rule.risk),
                risk=rule.risk,
                rule_id=rule.rule_id,
                rule_effect=rule.effect,
                sandbox_profile=profile,
            )
        if explicit_policy == CapabilityManager.ASK_EACH_TIME:
            return ShellPolicyDecision(
                False,
                True,
                "explicit command capability requires approval",
                explicit_policy,
                matched_rule=rule_match.matched_argv,
                high_risk=self._is_high_risk(rule.risk),
                risk=rule.risk,
                rule_id=rule.rule_id,
                rule_effect=rule.effect,
                sandbox_profile=profile,
            )
        if explicit_policy in {CapabilityManager.ALWAYS_ALLOW, CapabilityManager.ALLOW_ONCE}:
            return ShellPolicyDecision(
                allowed=True,
                ask_human=False,
                reason="explicit command capability allowed command",
                policy_level=explicit_policy,
                consume_once=explicit_decision.consume_capability_id is not None,
                consume_capability_id=explicit_decision.consume_capability_id,
                matched_rule=rule_match.matched_argv,
                high_risk=self._is_high_risk(rule.risk),
                risk=rule.risk,
                rule_id=rule.rule_id,
                rule_effect=rule.effect,
                sandbox_profile=profile,
            )

        if not policy_caps:
            raise CapabilityDenied(f"{pid} lacks shell execute policy for {resource}")

        level = self._selected_policy_level(policy_caps)
        if level in {self.config.shell.allowlist_auto_else_ask_level, self.config.shell.blocklist_ask_else_auto_level}:
            if rule.effect == CapabilityEffect.ALLOW:
                return ShellPolicyDecision(
                    allowed=True,
                    ask_human=False,
                    reason=rule.description or "shell rule allowed command",
                    policy_level=level,
                    matched_rule=rule_match.matched_argv,
                    high_risk=self._is_high_risk(rule.risk),
                    risk=rule.risk,
                    rule_id=rule.rule_id,
                    rule_effect=rule.effect,
                    sandbox_profile=profile,
                )
            return ShellPolicyDecision(
                allowed=False,
                ask_human=True,
                reason=rule.description or "shell rule requires approval",
                policy_level=level,
                matched_rule=rule_match.matched_argv,
                high_risk=self._is_high_risk(rule.risk),
                risk=rule.risk,
                rule_id=rule.rule_id,
                rule_effect=rule.effect,
                sandbox_profile=profile,
            )
        if level == self.config.shell.always_allow_level:
            return ShellPolicyDecision(
                allowed=True,
                ask_human=False,
                reason="shell policy is always_allow",
                policy_level=level,
                matched_rule=rule_match.matched_argv,
                high_risk=self._is_high_risk(rule.risk),
                risk=rule.risk,
                rule_id=rule.rule_id,
                rule_effect=rule.effect,
                sandbox_profile=profile,
            )
        return ShellPolicyDecision(
            False,
            False,
            f"unsupported shell policy level: {level}",
            level,
            matched_rule=rule_match.matched_argv,
            high_risk=self._is_high_risk(rule.risk),
            risk=rule.risk,
            rule_id=rule.rule_id,
            rule_effect=rule.effect,
            sandbox_profile=profile,
        )

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
                    "constraints": self._approval_constraints(argv, decision, timeout=timeout, cwd=os.fspath(cwd) if cwd is not None else "."),
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
                    "risk": decision.risk.value,
                    "rule_id": decision.rule_id,
                    "rule_effect": decision.rule_effect.value,
                    "sandbox_profile": self._profile_json(decision.sandbox_profile),
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
    ) -> Any:
        if self.events is None:
            return None
        return self.events.emit(
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
                "risk": decision.risk.value,
                "rule_id": decision.rule_id,
                "cwd": os.fspath(cwd) if cwd is not None else None,
            },
        )

    def _configured_rules(self) -> list[AuthorityRule]:
        rules: list[AuthorityRule] = list(self.config.shell.rules)
        for rule in self.config.shell.whitelist:
            rules.append(
                AuthorityRule(
                    rule_id=f"shell.config.allow.{'.'.join(rule.argv)}",
                    operation="shell.run",
                    effect=CapabilityEffect.ALLOW,
                    risk=AuthorityRisk.HARMLESS,
                    conditions={"argv": list(rule.argv), "match": rule.match},
                    description=rule.description or "configured harmless shell allow rule",
                )
            )
        for rule in self.config.shell.blacklist:
            rules.append(
                AuthorityRule(
                    rule_id=f"shell.config.ask.{'.'.join(rule.argv)}",
                    operation="shell.run",
                    effect=CapabilityEffect.ASK,
                    risk=AuthorityRisk.HIGH,
                    conditions={"argv": list(rule.argv), "match": rule.match},
                    description=rule.description or "configured high-risk shell ask rule",
                )
            )
        return rules

    def _is_high_risk(self, risk: AuthorityRisk) -> bool:
        return risk in {AuthorityRisk.HIGH, AuthorityRisk.DESTRUCTIVE}

    def _profile_json(self, profile: SandboxProfile | None) -> dict[str, Any] | None:
        if profile is None:
            return None
        return {
            "operation": profile.operation,
            "resource": profile.resource,
            "effect": profile.effect.value,
            "risk": profile.risk.value,
            "rule_id": profile.rule_id,
            "restrictions": profile.restrictions,
        }

    def _operation_context(
        self,
        pid: str,
        argv: list[str],
        resource: str,
        *,
        timeout: float,
        cwd: str,
        profile: SandboxProfile,
    ) -> dict[str, Any]:
        argv_json = "\0".join(argv)
        profile_json = self._profile_json(profile)
        return {
            "adapter": "shell",
            "primitive": "runtime.shell.run",
            "operation": "shell.run",
            "authority_operation": "shell.run",
            "pid": pid,
            "argv": list(argv),
            "argv_sha256": hashlib.sha256(argv_json.encode("utf-8")).hexdigest(),
            "command": argv[0],
            "resource": resource,
            "right": CapabilityRight.EXECUTE.value,
            "cwd": cwd,
            "timeout_s": timeout,
            "risk": profile.risk.value,
            "rule_id": profile.rule_id,
            "rule_effect": profile.effect.value,
            "sandbox_profile": profile_json,
            "network": bool(profile_json and profile_json["restrictions"].get("network")),
            "filesystem_intent": profile_json["restrictions"].get("filesystem_intent") if profile_json else None,
        }

    def _approval_constraints(
        self,
        argv: list[str],
        decision: ShellPolicyDecision,
        *,
        timeout: float,
        cwd: str,
    ) -> dict[str, Any]:
        argv_json = "\0".join(argv)
        return {
            AUTHORITY_RULES_KEY: [
                {
                    "rule_id": f"shell.approval.{decision.rule_id or 'exact'}",
                    "operation": "shell.run",
                    "effect": CapabilityEffect.ALLOW.value,
                    "risk": decision.risk.value,
                    "conditions": {
                        "argv": list(argv),
                        "match": "exact",
                        "argv_sha256": hashlib.sha256(argv_json.encode("utf-8")).hexdigest(),
                        "cwd": cwd,
                        "timeout_s": timeout,
                    },
                    "description": "one-shot human approval for exact shell argv",
                }
            ]
        }

    def _matching_shell_policy_caps(self, pid: str, resource: str, context: dict[str, Any]) -> list[Capability]:
        caps = [
            cap
            for cap in self.capabilities.matching_capabilities(
                pid,
                resource,
                CapabilityRight.EXECUTE,
                include_ask=True,
            )
            if self.config.shell.policy_capability_key in cap.constraints
        ]
        caps = [
            cap
            for cap in caps
            if cap.effect == CapabilityEffect.DENY
            or self.capabilities.constraints_satisfied(cap, context)
        ]
        caps.sort(key=lambda cap: (len(cap.resource), cap.issued_at), reverse=True)
        return caps

    def _explicit_command_decision(self, pid: str, resource: str, context: dict[str, Any]) -> Any:
        """Classify direct `shell:<command>` authority separate from policy caps.

        A shell policy capability decides how whitelist/blacklist rules are
        applied. A direct command capability is narrower authority granted by
        human approval or bootstrap. Both are canonical Capability records and both
        use the central resource matcher; they are split only so a broad
        `shell:*` policy does not bypass shell-specific token checks.
        """

        caps = [
            cap
            for cap in self.capabilities.matching_capabilities(
                pid,
                resource,
                CapabilityRight.EXECUTE,
                include_ask=True,
            )
            if self.config.shell.policy_capability_key not in cap.constraints
        ]
        # A broad `shell:*` allow is a policy grant only when it carries
        # shell_policy_level. Treating it as direct command authority would turn
        # a registry-level wildcard into an unreviewed "always allow" shell.
        caps = [
            cap
            for cap in caps
            if cap.resource != self.config.shell.policy_resource or cap.effect != CapabilityEffect.ALLOW
        ]
        caps = [
            cap
            for cap in caps
            if self._direct_shell_capability_applies_to_rule(cap, context)
        ]
        if not caps:
            return self.capabilities.authorize_matching_capabilities(pid, resource, CapabilityRight.EXECUTE, [], context)
        caps.sort(key=lambda cap: (len(cap.resource), cap.issued_at), reverse=True)
        return self.capabilities.authorize_matching_capabilities(pid, resource, CapabilityRight.EXECUTE, caps, context)

    def _direct_shell_capability_applies_to_rule(self, cap: Capability, context: dict[str, Any]) -> bool:
        if cap.effect != CapabilityEffect.ALLOW:
            return True
        if AUTHORITY_RULES_KEY in cap.constraints:
            return True
        # Bare `shell:<command>` grants are command-family hints, not permission
        # to run every subcommand. Without AuthorityRule constraints they only
        # cover argv that the deterministic classifier already considers
        # harmless/low-risk allow; medium/high commands need an explicit rule or
        # a shell policy approved by the human.
        return context.get("rule_effect") == CapabilityEffect.ALLOW.value

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

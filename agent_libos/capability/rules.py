from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Iterable

from agent_libos.models import AuthorityRisk, AuthorityRule, CapabilityEffect
from agent_libos.models.exceptions import ValidationError

AUTHORITY_RULES_KEY = "authority_rules"

_AUTHORITY_RULE_FIELDS = frozenset(
    {
        "rule_id",
        "operation",
        "effect",
        "risk",
        "conditions",
        "description",
    }
)

_WINDOWS_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com", ".ps1")
_NETWORK_EXECUTABLES = {"curl", "wget", "ssh", "scp", "sftp", "nc", "ncat", "netcat"}
_SCRIPT_EXECUTABLES = {
    "bash",
    "sh",
    "zsh",
    "fish",
    "cmd",
    "powershell",
    "pwsh",
    "python",
    "python3",
    "py",
    "node",
    "ruby",
    "perl",
    "php",
}
_PACKAGE_EXECUTABLES = {"npm", "npx", "yarn", "pnpm", "pip", "pip3", "uv", "cargo"}
_DESTRUCTIVE_EXECUTABLES = {
    "rm",
    "del",
    "rmdir",
    "remove-item",
    "move-item",
    "chmod",
    "chown",
    "icacls",
    "reg",
    "regedit",
    "taskkill",
    "sudo",
    "su",
    "runas",
    "docker",
    "kubectl",
}
_HIGH_PACKAGE_SUBCOMMANDS = {
    "install",
    "add",
    "remove",
    "uninstall",
    "update",
    "upgrade",
    "publish",
    "run",
    "exec",
}
_SHELL_SYNTAX_PATTERNS = (
    re.compile(r"[\r\n;|&<>#`'\"]"),
    re.compile(r"\$\(|\$\{"),
    re.compile(r"<\(|>\("),
    re.compile(r"\|\||&&"),
    re.compile(r"\{[^}]*,[^}]*\}"),
)


@dataclass(frozen=True)
class RuleMatch:
    rule: AuthorityRule
    matched_argv: tuple[str, ...] | None = None


class AuthorityRuleCodec:
    """Strict codec for rules stored in capability constraints."""

    def coerce_many(self, value: Any) -> list[AuthorityRule]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValidationError("authority rules must be a list")
        return [self.coerce(item) for item in value]

    def coerce(self, value: AuthorityRule | dict[str, Any]) -> AuthorityRule:
        if isinstance(value, AuthorityRule):
            return value
        if not isinstance(value, dict):
            raise ValidationError("authority rule must be a mapping")
        unknown_fields = sorted(str(field) for field in value if field not in _AUTHORITY_RULE_FIELDS)
        if unknown_fields:
            raise ValidationError(f"authority rule has unknown fields: {', '.join(unknown_fields)}")
        try:
            conditions = value.get("conditions") or {}
            if not isinstance(conditions, dict):
                raise ValidationError("authority rule conditions must be a mapping")
            return AuthorityRule(
                rule_id=self._string(value.get("rule_id"), "rule_id"),
                operation=self._string(value.get("operation"), "operation"),
                effect=CapabilityEffect(str(value.get("effect", CapabilityEffect.ASK.value))),
                risk=AuthorityRisk(str(value.get("risk", AuthorityRisk.MEDIUM.value))),
                conditions=dict(conditions),
                description=str(value.get("description") or ""),
            )
        except ValueError as exc:
            raise ValidationError(f"invalid authority rule: {exc}") from exc

    def to_json(self, rule: AuthorityRule | dict[str, Any]) -> dict[str, Any]:
        selected = self.coerce(rule)
        return {
            "rule_id": selected.rule_id,
            "operation": selected.operation,
            "effect": selected.effect.value,
            "risk": selected.risk.value,
            "conditions": dict(selected.conditions),
            "description": selected.description,
        }

    def _string(self, value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"authority rule {field} must be a non-empty string")
        return value.strip()


class ShellRuleEngine:
    """Deterministic argv classifier used before shell provider execution."""

    def __init__(self, custom_rules: Iterable[AuthorityRule | dict[str, Any]] = ()) -> None:
        codec = AuthorityRuleCodec()
        self.custom_rules = [codec.coerce(rule) for rule in custom_rules]

    def classify(self, argv: list[str]) -> RuleMatch:
        path_qualified_argv0 = self.argv0_has_path(argv[0]) if argv else False
        normalized = self._normalize_argv(argv)
        direct = normalized[0]
        nested = self._nested_executables(normalized)
        destructive = self._first_in({direct, *nested}, _DESTRUCTIVE_EXECUTABLES)
        if destructive is not None:
            return self._match(
                "shell.destructive.default",
                CapabilityEffect.DENY,
                AuthorityRisk.DESTRUCTIVE,
                (destructive,),
                "destructive shell command is denied by default",
            )

        network = self._first_in({direct, *nested}, _NETWORK_EXECUTABLES)
        if network is not None:
            return self._match(
                "shell.network.default",
                CapabilityEffect.ASK,
                AuthorityRisk.HIGH,
                (network,),
                "network-capable shell command requires approval",
            )

        if self._is_medium_risk(normalized):
            return self._guard_auto_allow(
                argv,
                self._match(
                    f"shell.medium.{direct}",
                    CapabilityEffect.ASK,
                    AuthorityRisk.MEDIUM,
                    tuple(normalized[: min(len(normalized), 3)]),
                    "project code execution requires approval",
                ),
            )

        if self._is_high_risk_workspace_write(normalized):
            return self._guard_auto_allow(
                argv,
                self._match(
                    "shell.high.compileall",
                    CapabilityEffect.ASK,
                    AuthorityRisk.HIGH,
                    tuple(normalized[: min(len(normalized), 3)]),
                    "shell command writes project build artifacts and requires approval",
                ),
            )

        package = self._first_in({direct, *nested}, _PACKAGE_EXECUTABLES)
        if package is not None:
            return self._guard_auto_allow(
                argv,
                self._match(
                    "shell.package-manager.default",
                    CapabilityEffect.ASK,
                    AuthorityRisk.HIGH,
                    (package,),
                    "package manager command requires approval",
                ),
            )

        script = self._first_in({direct, *nested}, _SCRIPT_EXECUTABLES)
        if script is not None:
            return self._guard_auto_allow(
                argv,
                self._match(
                    "shell.interpreter.default",
                    CapabilityEffect.ASK,
                    AuthorityRisk.HIGH,
                    (script,),
                    "script interpreter command requires approval",
                ),
            )

        for rule in self.custom_rules:
            if self._matches_rule(argv, normalized, rule):
                return self._guard_auto_allow(argv, RuleMatch(rule=rule, matched_argv=self._rule_argv(rule)))

        if not path_qualified_argv0 and self._is_harmless(normalized):
            return self._guard_auto_allow(
                argv,
                self._match(
                    f"shell.harmless.{direct}",
                    CapabilityEffect.ALLOW,
                    AuthorityRisk.HARMLESS,
                    tuple(normalized),
                    "harmless read-only inspection command",
                ),
            )

        if not path_qualified_argv0 and self._is_low_risk(normalized):
            return self._guard_auto_allow(
                argv,
                self._match(
                    f"shell.low.{direct}",
                    CapabilityEffect.ALLOW,
                    AuthorityRisk.LOW,
                    tuple(normalized[: min(len(normalized), 3)]),
                    "low-risk read-only project inspection command",
                ),
            )

        return self._guard_auto_allow(
            argv,
            self._match(
                "shell.unknown.default",
                CapabilityEffect.ASK,
                AuthorityRisk.MEDIUM,
                (direct,),
                "unclassified shell command requires approval",
            ),
        )

    def _matches_rule(self, raw_argv: list[str], argv: list[str], rule: AuthorityRule) -> bool:
        if rule.operation != "shell.run":
            return False
        conditions = rule.conditions
        rule_argv = conditions.get("argv")
        if rule_argv is not None:
            if not isinstance(rule_argv, list) or not all(isinstance(item, str) for item in rule_argv):
                return False
            if raw_argv and rule_argv:
                rule_has_path = self.argv0_has_path(rule_argv[0])
                if self.argv0_has_path(raw_argv[0]) and not rule_has_path:
                    return False
            expected = [self.normalize_executable(rule_argv[0]), *rule_argv[1:]] if rule_argv else []
            match = str(conditions.get("match", "exact"))
            if match == "exact":
                return argv == expected
            if match == "prefix":
                return len(argv) >= len(expected) and argv[: len(expected)] == expected
            return False
        regex = conditions.get("regex_token")
        if isinstance(regex, str):
            try:
                pattern = re.compile(regex)
            except re.error:
                return False
            return any(pattern.fullmatch(token) for token in argv)
        return False

    def _normalize_argv(self, argv: list[str]) -> list[str]:
        if not argv:
            return []
        return [self.normalize_executable(argv[0]), *argv[1:]]

    def normalize_executable(self, value: str) -> str:
        raw = value.strip().replace("\\", "/")
        name = PurePath(raw).name or raw
        lowered = name.casefold()
        for suffix in _WINDOWS_EXECUTABLE_SUFFIXES:
            if lowered.endswith(suffix):
                return lowered[: -len(suffix)]
        return lowered

    def argv0_has_path(self, value: str) -> bool:
        return "/" in value or "\\" in value or PurePath(value).is_absolute()

    def _nested_executables(self, argv: list[str]) -> set[str]:
        executables = _SCRIPT_EXECUTABLES | _NETWORK_EXECUTABLES | _PACKAGE_EXECUTABLES | _DESTRUCTIVE_EXECUTABLES
        nested: set[str] = set()
        for token in argv[1:]:
            normalized = self.normalize_executable(token)
            if normalized in executables:
                nested.add(normalized)
        return nested

    def _is_harmless(self, argv: list[str]) -> bool:
        harmless = {
            ("git", "status"),
            ("git", "status", "--short"),
            ("git", "branch", "--show-current"),
            ("git", "rev-parse", "--show-toplevel"),
            ("git", "diff", "--stat"),
            ("python", "--version"),
            ("python3", "--version"),
            ("py", "--version"),
            ("node", "--version"),
            ("npm", "--version"),
            ("uv", "--version"),
        }
        return tuple(argv) in harmless

    def _is_low_risk(self, argv: list[str]) -> bool:
        if argv == ["git", "diff"]:
            return True
        return False

    def _is_medium_risk(self, argv: list[str]) -> bool:
        if argv[0] == "pytest":
            return True
        if argv[:2] == ["npm", "test"]:
            return True
        if argv[:2] == ["uv", "run"]:
            return True
        return False

    def _is_high_risk_workspace_write(self, argv: list[str]) -> bool:
        return argv[:3] in (["python", "-m", "compileall"], ["python3", "-m", "compileall"], ["py", "-m", "compileall"])

    def _first_in(self, values: set[str], candidates: set[str]) -> str | None:
        for candidate in sorted(candidates):
            if candidate in values:
                return candidate
        return None

    def _rule_argv(self, rule: AuthorityRule) -> tuple[str, ...] | None:
        argv = rule.conditions.get("argv")
        if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
            return tuple(argv)
        return None

    def _guard_auto_allow(self, raw_argv: list[str], match: RuleMatch) -> RuleMatch:
        if match.rule.effect == CapabilityEffect.DENY:
            return match
        token = self._first_shell_syntax_token(raw_argv)
        if token is None:
            return match
        # Non always_allow policies must not silently accept shell metasyntax.
        # The provider still executes with shell=False, but downgrading to ask
        # prevents future prefix allow rules from becoming parser-mismatch bugs
        # if a called program later feeds an argument into a shell.
        return self._match(
            "shell.syntax.default",
            CapabilityEffect.ASK,
            AuthorityRisk.HIGH,
            (token,),
            "shell metasyntax in argv requires approval",
        )

    def _first_shell_syntax_token(self, argv: list[str]) -> str | None:
        for token in argv:
            if any(pattern.search(token) for pattern in _SHELL_SYNTAX_PATTERNS):
                return token
        return None

    def _match(
        self,
        rule_id: str,
        effect: CapabilityEffect,
        risk: AuthorityRisk,
        matched_argv: tuple[str, ...],
        description: str,
    ) -> RuleMatch:
        return RuleMatch(
            rule=AuthorityRule(
                rule_id=rule_id,
                operation="shell.run",
                effect=effect,
                risk=risk,
                conditions={"argv": list(matched_argv), "match": "exact"},
                description=description,
            ),
            matched_argv=matched_argv,
        )

from __future__ import annotations

from typing import Any

from agent_libos.models import AuthorityRisk, CapabilityEffect, SandboxProfile


class SandboxProfileBuilder:
    """Build primitive-facing sandbox profiles from authorization decisions."""

    def filesystem(
        self,
        *,
        resource: str,
        right: str,
        effect: CapabilityEffect,
        risk: AuthorityRisk = AuthorityRisk.LOW,
        path: str | None = None,
    ) -> SandboxProfile:
        return SandboxProfile(
            operation=f"filesystem.{right}",
            resource=resource,
            effect=effect,
            risk=risk,
            restrictions={"path": path, "right": right},
        )

    def shell(
        self,
        *,
        resource: str,
        effect: CapabilityEffect,
        risk: AuthorityRisk,
        rule_id: str | None,
        argv: list[str],
        timeout_s: float,
        cwd: str | None,
        extra: dict[str, Any] | None = None,
    ) -> SandboxProfile:
        return SandboxProfile(
            operation="shell.run",
            resource=resource,
            effect=effect,
            risk=risk,
            rule_id=rule_id,
            restrictions={
                "argv": list(argv),
                "timeout_s": timeout_s,
                "cwd": cwd,
                "shell": False,
                "network": risk in {AuthorityRisk.HIGH, AuthorityRisk.DESTRUCTIVE},
                "filesystem_intent": self._filesystem_intent(argv, risk),
                **dict(extra or {}),
            },
        )

    def deno_jit(self, *, resource: str = "deno:jit_sandbox") -> SandboxProfile:
        return SandboxProfile(
            operation="deno.syscall",
            resource=resource,
            effect=CapabilityEffect.ASK,
            risk=AuthorityRisk.HIGH,
            restrictions={
                "allow_fs": False,
                "allow_net": False,
                "allow_env": False,
                "allow_run": False,
                "libos_syscall_only": True,
            },
        )

    def jsonrpc(
        self,
        *,
        resource: str,
        effect: CapabilityEffect,
        risk: AuthorityRisk = AuthorityRisk.HIGH,
        endpoint_id: str,
        method_id: str,
    ) -> SandboxProfile:
        return SandboxProfile(
            operation="jsonrpc.call",
            resource=resource,
            effect=effect,
            risk=risk,
            restrictions={
                "registered_endpoint_only": True,
                "endpoint_id": endpoint_id,
                "method_id": method_id,
                "caller_supplied_url": False,
            },
        )

    def _filesystem_intent(self, argv: list[str], risk: AuthorityRisk) -> str:
        if risk == AuthorityRisk.DESTRUCTIVE:
            return "delete_or_mutate"
        if risk == AuthorityRisk.HIGH:
            return "write_or_external"
        if risk in {AuthorityRisk.HARMLESS, AuthorityRisk.LOW}:
            return "read"
        return "execute"

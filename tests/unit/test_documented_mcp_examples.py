"""Exercise the documented MCP examples without credentials or network I/O."""

from __future__ import annotations

import json
import re
import socket
from functools import wraps
from pathlib import Path
from typing import Any

import pytest

from agent_libos import Runtime
from agent_libos.mcp.oauth import (
    McpOAuthError,
    McpOAuthRegistrationMode,
    McpOAuthTokenEndpointAuthMethod,
    mcp_oauth_profile_from_mapping,
)
from agent_libos.mcp.types import McpResource
from agent_libos.primitives.mcp import McpPrimitive
from examples.mcp import run_pagination_e2e


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("The documented offline MCP example attempted network I/O")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def test_documented_mcp_pagination_continues_after_filtered_empty_page(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed_pages: list[tuple[int, bool]] = []
    original = McpPrimitive.list_resources

    @wraps(original)
    def observe(
        self: McpPrimitive, server_id: str, **kwargs: Any
    ) -> Any:
        page = original(self, server_id, **kwargs)
        observed_pages.append((len(page.items), page.has_more))
        return page

    monkeypatch.setattr(McpPrimitive, "list_resources", observe)

    assert run_pagination_e2e.main() == 0

    output = capsys.readouterr()
    assert output.err == ""
    report = json.loads(output.out)
    assert observed_pages == [(0, True), (1, True), (1, False)]
    assert report["pages_read"] == 3
    assert report["resources"] == ["status", "version"]
    assert report["status"]["kind"] == "complete"
    assert report["status"]["value"]["resource_id"] == "status"
    assert report["status"]["value"]["provenance"] == "untrusted_mcp_resource"
    assert report["status"]["value"]["contents"][0]["text"] == "ready"


def test_documented_mcp_pagination_budget_rejects_partial_catalog(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = run_pagination_e2e.collect_resources

    def collect_with_smaller_budget(
        runtime: Runtime, server_id: str, *, max_pages: int
    ) -> list[McpResource]:
        # Keep the example's real Runtime/provider setup and exhaust the
        # traversal after it has received one allowed item and another cursor.
        return original(runtime, server_id, max_pages=max_pages - 1)

    monkeypatch.setattr(
        run_pagination_e2e, "collect_resources", collect_with_smaller_budget
    )

    with pytest.raises(RuntimeError, match="exceeds this traversal's page budget"):
        run_pagination_e2e.main()

    assert capsys.readouterr().out == ""


def test_documented_mcp_oauth_profile_uses_strict_non_secret_shape() -> None:
    supplied = json.loads((ROOT / "examples/mcp/oauth-profile.json").read_text())
    documentation = (ROOT / "docs/mcp.md").read_text()
    section = documentation.split("### OAuth profile file\n", 1)[1].split("\n## ", 1)[0]
    code_block = re.search(r"```json\n(.*?)\n```", section, re.DOTALL)
    assert code_block is not None
    assert json.loads(code_block.group(1)) == supplied

    profile = mcp_oauth_profile_from_mapping(supplied)

    assert profile.profile_id == "work-oauth"
    assert profile.server_id == "demo-mcp"
    assert profile.registration_mode is McpOAuthRegistrationMode.PREREGISTERED
    assert profile.token_endpoint_auth_method is McpOAuthTokenEndpointAuthMethod.NONE
    assert profile.default_scopes == ("resources.read",)
    assert profile.default_scopes == profile.allowed_scopes
    assert profile.allow_loopback_http is False
    with pytest.raises(McpOAuthError, match="profile fields are invalid"):
        mcp_oauth_profile_from_mapping({**supplied, "client_secret": "not-allowed"})

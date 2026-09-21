#!/usr/bin/env python3
"""Traverse a governed Resource catalog in one Runtime without network I/O."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from agent_libos import Runtime
from agent_libos.mcp.manifest import McpResourceSpec, parse_mcp_v3_manifest_yaml_text
from agent_libos.mcp.types import (
    McpComplete,
    McpPage,
    McpResource,
    McpResourceContents,
    McpResourceTemplate,
    McpTextContent,
)
from agent_libos.models.mcp import McpServerSpec
from agent_libos.substrate import LocalResourceProviderSubstrate
from agent_libos.utils.serde import to_jsonable


EXAMPLE_ROOT = Path(__file__).resolve().parent


class PaginatedResourceProvider:
    """Complete public Resource SPI; all responses are deterministic local data."""

    mcp_manifest_schema_version = 3
    mcp_protocol_revision = "2026-07-28"

    def __init__(self) -> None:
        self.list_calls = 0

    async def list_resources(
        self, server: McpServerSpec, cursor: str | None, *, deadline: float
    ) -> McpPage[McpResource]:
        del server
        _check_deadline(deadline)
        self.list_calls += 1
        if cursor is None:
            # This first page becomes empty after the manifest allowlist filter.
            return McpPage(
                items=(McpResource(resource_id="demo://undeclared", name="Hidden"),),
                next_cursor="provider-page-2",
            )
        if cursor == "provider-page-2":
            return McpPage(
                items=(McpResource(resource_id="demo://status", name="Status"),),
                next_cursor="provider-page-3",
            )
        if cursor == "provider-page-3":
            return McpPage(
                items=(McpResource(resource_id="demo://version", name="Version"),)
            )
        raise ValueError("unexpected provider cursor")

    async def list_resource_templates(
        self, server: McpServerSpec, cursor: str | None, *, deadline: float
    ) -> McpPage[McpResourceTemplate]:
        del server, cursor
        _check_deadline(deadline)
        return McpPage(items=())

    async def read_resource(
        self,
        server: McpServerSpec,
        resource_name: str,
        variables: Mapping[str, str] | None,
        *,
        deadline: float,
    ) -> McpComplete[McpResourceContents]:
        del server, variables
        _check_deadline(deadline)
        values = {"demo://status": "ready", "demo://version": "example-v1"}
        return McpComplete(
            value=McpResourceContents(
                resource_id=resource_name,
                contents=(McpTextContent(text=values[resource_name]),),
            )
        )


def collect_resources(
    runtime: Runtime, server_id: str, *, max_pages: int
) -> list[McpResource]:
    """Return a complete catalog or raise instead of returning a partial scan."""
    if type(max_pages) is not int or max_pages <= 0:
        raise ValueError("max_pages must be a positive integer")
    resources: list[McpResource] = []
    cursor = None
    for _ in range(max_pages):
        page = runtime.mcp.list_resources(server_id, cursor=cursor, actor="runtime")
        resources.extend(page.items)
        if page.next_cursor is None:
            return resources
        cursor = page.next_cursor
    raise RuntimeError("resource catalog exceeds this traversal's page budget")


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("Resource provider deadline elapsed")


def main() -> int:
    manifest = parse_mcp_v3_manifest_yaml_text(
        (EXAMPLE_ROOT / "http-v3.yaml").read_text(encoding="utf-8")
    )
    manifest = replace(
        manifest,
        server_id="demo-pagination",
        tools=(),
        resource_templates=(),
        prompts=(),
        resources=(
            McpResourceSpec(resource_id="status", remote_uri="demo://status"),
            McpResourceSpec(resource_id="version", remote_uri="demo://version"),
        ),
    )
    provider = PaginatedResourceProvider()
    substrate = LocalResourceProviderSubstrate(EXAMPLE_ROOT.parents[1])
    substrate.mcp_resource_provider = provider
    runtime = Runtime.open(":memory:", substrate=substrate)
    try:
        runtime.mcp.register_server(manifest, actor="runtime", require_capability=False)
        resources = collect_resources(runtime, manifest.server_id, max_pages=3)
        assert [item.resource_id for item in resources] == ["status", "version"]
        assert provider.list_calls == 3
        result = runtime.mcp.read_resource(manifest.server_id, "status", actor="runtime")
        assert isinstance(result, McpComplete)
        assert result.value is not None and result.value.resource_id == "status"
        assert result.value.contents == (McpTextContent(text="ready"),)
        print(
            json.dumps(
                {
                    "pages_read": provider.list_calls,
                    "resources": [item.resource_id for item in resources],
                    "status": to_jsonable(result),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())

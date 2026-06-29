from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agent_libos import Runtime
from agent_libos.models import CapabilityRight, McpHttpTransportSpec, McpServerSpec
from agent_libos.substrate import LocalResourceProviderSubstrate, SdkMcpProvider

pytestmark = pytest.mark.mcp


class TestMcpSdkIntegration:
    def test_stdio_fastmcp_tool_call(self, tmp_path: Path) -> None:
        server_path = _write_fastmcp_stdio_server(tmp_path)
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp stdio integration")
            runtime.mcp.register_server(_server_spec("stdio-it", sys.executable, [str(server_path)]), actor="cli", require_capability=False)
            runtime.capability.grant(pid, "mcp:stdio-it:echo", [CapabilityRight.READ], issued_by="test")

            result = runtime.mcp.call_tool(pid, "stdio-it", "echo", {"text": "hello"})

            assert result.ok
            assert "hello" in json.dumps(result.result, sort_keys=True)
        finally:
            runtime.close()

    def test_stdio_uses_workspace_cwd_and_exact_allowlisted_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = tmp_path / "workspace"
        cwd = workspace / "server-cwd"
        cwd.mkdir(parents=True)
        server_path = _write_fastmcp_env_server(tmp_path)
        monkeypatch.setenv("AGENT_LIBOS_MCP_ALLOWED_TOKEN", "allowed-token")
        monkeypatch.setenv("OPENAI_API_KEY", "should-not-inherit")
        runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(workspace))
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="mcp stdio env integration")
            spec = _server_spec("stdio-env-it", sys.executable, [str(server_path)])
            spec["stdio"]["cwd"] = "server-cwd"
            spec["stdio"]["env"] = {"DEMO_TOKEN": "AGENT_LIBOS_MCP_ALLOWED_TOKEN"}
            spec["tools"] = [_tool_spec(tool_id="envcwd", mcp_name="demo.envcwd")]
            runtime.mcp.register_server(spec, actor="cli", require_capability=False)
            runtime.capability.grant(pid, "mcp:stdio-env-it:envcwd", [CapabilityRight.READ], issued_by="test")

            result = runtime.mcp.call_tool(pid, "stdio-env-it", "envcwd", {})

            assert result.ok
            structured = result.result["structured_content"]
            assert Path(structured["cwd"]).resolve() == cwd.resolve()
            assert structured["allowed"] == "allowed-token"
            assert structured["secret"] is None
        finally:
            runtime.close()

    def test_streamable_http_fastmcp_tool_call(self, tmp_path: Path) -> None:
        port = _free_local_port()
        server_path = _write_fastmcp_http_server(tmp_path)
        proc = subprocess.Popen(
            [sys.executable, str(server_path), str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_for_port(port)
            runtime = Runtime.open("local")
            try:
                pid = runtime.process.spawn(image="base-agent:v0", goal="mcp http integration")
                runtime.mcp.register_server(_http_server_spec("http-it", f"http://127.0.0.1:{port}/mcp"), actor="cli", require_capability=False)
                runtime.capability.grant(pid, "mcp:http-it:echo", [CapabilityRight.READ], issued_by="test")

                result = runtime.mcp.call_tool(pid, "http-it", "echo", {"text": "hello"})

                assert result.ok
                assert "hello" in json.dumps(result.result, sort_keys=True)
            finally:
                runtime.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def test_streamable_http_client_does_not_follow_redirects(self) -> None:
        port = _free_local_port()
        handler = _redirect_handler(f"http://127.0.0.1:{port}/private")
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        provider = SdkMcpProvider()
        try:
            spec = McpServerSpec(
                schema_version=1,
                server_id="redirect-it",
                transport="streamable_http",
                http=McpHttpTransportSpec(url=f"http://127.0.0.1:{port}/mcp"),
                tools=[],
                timeout_s=5,
                max_request_bytes=65536,
                max_response_bytes=1048576,
            )

            async def request_redirect() -> int:
                async with provider._http_client(spec, timeout_s=5) as client:
                    response = await client.get(f"http://127.0.0.1:{port}/redirect")
                    return int(response.status_code)

            status = asyncio.run(request_redirect())

            assert status == 307
            assert handler.paths == ["/redirect"]
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def _write_fastmcp_stdio_server(root: Path) -> Path:
    path = root / "stdio_server.py"
    path.write_text(
        """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("stdio-it")

@mcp.tool(name="demo.echo")
def echo(text: str) -> dict[str, str]:
    return {"echo": text}

if __name__ == "__main__":
    mcp.run("stdio")
""".strip(),
        encoding="utf-8",
    )
    return path


def _write_fastmcp_env_server(root: Path) -> Path:
    path = root / "env_server.py"
    path.write_text(
        """
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("stdio-env-it")

@mcp.tool(name="demo.envcwd")
def envcwd() -> dict[str, str | None]:
    return {
        "cwd": os.getcwd(),
        "allowed": os.environ.get("DEMO_TOKEN"),
        "secret": os.environ.get("OPENAI_API_KEY"),
    }

if __name__ == "__main__":
    mcp.run("stdio")
""".strip(),
        encoding="utf-8",
    )
    return path


def _write_fastmcp_http_server(root: Path) -> Path:
    path = root / "http_server.py"
    path.write_text(
        """
from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("http-it", host="127.0.0.1", port=int(sys.argv[1]), streamable_http_path="/mcp", stateless_http=True)

@mcp.tool(name="demo.echo")
def echo(text: str) -> dict[str, str]:
    return {"echo": text}

if __name__ == "__main__":
    mcp.run("streamable-http")
""".strip(),
        encoding="utf-8",
    )
    return path


def _server_spec(server_id: str, command: str, args: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "server_id": server_id,
        "transport": "stdio",
        "stdio": {"command": command, "args": args},
        "tools": [_tool_spec()],
        "timeout_s": 10,
        "max_request_bytes": 65536,
        "max_response_bytes": 1048576,
    }


def _http_server_spec(server_id: str, url: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "server_id": server_id,
        "transport": "streamable_http",
        "http": {"url": url},
        "tools": [_tool_spec()],
        "timeout_s": 10,
        "max_request_bytes": 65536,
        "max_response_bytes": 1048576,
    }


def _tool_spec(tool_id: str = "echo", mcp_name: str = "demo.echo") -> dict[str, Any]:
    return {
        "tool_id": tool_id,
        "mcp_name": mcp_name,
        "right": "read",
        "rollback_class": "no_rollback_required",
        "state_mutation": False,
        "information_flow": True,
    }


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise AssertionError(f"MCP test server did not listen on port {port}")


def _redirect_handler(location: str) -> type[BaseHTTPRequestHandler]:
    class RedirectHandler(BaseHTTPRequestHandler):
        paths: list[str] = []

        def do_GET(self) -> None:
            type(self).paths.append(self.path)
            if self.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", location)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"redirect followed")

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return RedirectHandler

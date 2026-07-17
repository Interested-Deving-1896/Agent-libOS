from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import Iterator

import pytest

from agent_libos.substrate.local import _McpPolicyAsyncHTTPTransport


httpx = pytest.importorskip("httpx")


def _handler(
    body: bytes,
    *,
    content_encoding: str | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        accept_encodings: list[str] = []

        def do_GET(self) -> None:  # noqa: N802
            type(self).accept_encodings.append(self.headers.get("Accept-Encoding", ""))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if content_encoding is not None:
                self.send_header("Content-Encoding", content_encoding)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return None

    return Handler


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


async def _get(url: str, *, max_response_bytes: int) -> bytes:
    transport = _McpPolicyAsyncHTTPTransport(
        max_response_bytes=max_response_bytes,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get(url)
        return bytes(response.content)


def test_public_httpcore_transport_round_trip_forces_identity_encoding() -> None:
    handler = _handler(b'{"ok":true}')

    with _serve(handler) as url:
        body = asyncio.run(_get(url, max_response_bytes=1024))

    assert body == b'{"ok":true}'
    assert handler.accept_encodings == ["identity"]


def test_http_transport_bounds_body_before_materialization() -> None:
    handler = _handler(b"x" * 32)

    with _serve(handler) as url:
        with pytest.raises(
            RuntimeError,
            match="MCP HTTP response exceeded max_response_bytes=16",
        ):
            asyncio.run(_get(url, max_response_bytes=16))


def test_http_transport_rejects_content_encoding_before_decode() -> None:
    handler = _handler(b"encoded", content_encoding="gzip")

    with _serve(handler) as url:
        with pytest.raises(
            RuntimeError,
            match="unsupported Content-Encoding=gzip",
        ):
            asyncio.run(_get(url, max_response_bytes=1024))

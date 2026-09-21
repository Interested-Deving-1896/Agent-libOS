"""Network denial for offline examples, including Windows asyncio wakeups."""

from __future__ import annotations

import socket
import threading
from functools import wraps
from typing import Any

import pytest


def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject connections and DNS, preserving only socketpair construction.

    Windows implements socketpair with a private loopback TCP connection, which
    asyncio needs for its wakeup pipe. Permit the implementation's synchronous
    connect on this thread without allowing arbitrary loopback destinations or
    temporarily restoring connect globally for concurrent threads.
    """
    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair
    scope = threading.local()

    def denied(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("The documented offline example attempted network I/O")

    def connect(sock: socket.socket, address: Any) -> None:
        if getattr(scope, "creating_socketpair", False):
            return original_connect(sock, address)
        denied()

    @wraps(original_socketpair)
    def socketpair(*args: Any, **kwargs: Any) -> tuple[socket.socket, socket.socket]:
        previous = getattr(scope, "creating_socketpair", False)
        scope.creating_socketpair = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            scope.creating_socketpair = previous

    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def _tcp_socketpair() -> tuple[socket.socket, socket.socket]:
    """Exercise the Windows socketpair connection path on every test host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.settimeout(5)
            client.connect(listener.getsockname())
            server, _ = listener.accept()
            client.settimeout(None)
            return server, client
        except BaseException:
            client.close()
            raise


@pytest.fixture(params=("native", "tcp-socketpair"))
def offline_network(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> None:
    if request.param == "tcp-socketpair":
        monkeypatch.setattr(socket, "socketpair", _tcp_socketpair)
    block_network(monkeypatch)

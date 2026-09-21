from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.support.network import block_network, offline_network


def test_offline_guard_allows_socketpair_but_rejects_connections(
    offline_network: None,
) -> None:
    reader, writer = socket.socketpair()
    with reader, writer:
        reader.settimeout(5)
        writer.sendall(b"wakeup")
        assert reader.recv(6) == b"wakeup"

    # Loopback is not an exemption: an offline example must not reach a local
    # provider or service either. Remote addresses are denied before any I/O.
    for address in (("127.0.0.1", 12345), ("192.0.2.1", 443)):
        with socket.socket() as sock:
            with pytest.raises(AssertionError, match="attempted network I/O"):
                sock.connect(address)
            with pytest.raises(AssertionError, match="attempted network I/O"):
                sock.connect_ex(address)
    with pytest.raises(AssertionError, match="attempted network I/O"):
        socket.getaddrinfo("example.test", 443)


def test_socketpair_exemption_is_thread_local_and_cleared_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def assert_connection_denied() -> None:
        with socket.socket() as sock:
            with pytest.raises(AssertionError, match="attempted network I/O"):
                sock.connect(("127.0.0.1", 12345))

    def failing_socketpair() -> tuple[socket.socket, socket.socket]:
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(assert_connection_denied).result(timeout=5)
        raise OSError("socketpair unavailable")

    monkeypatch.setattr(socket, "socketpair", failing_socketpair)
    block_network(monkeypatch)

    with pytest.raises(OSError, match="socketpair unavailable"):
        socket.socketpair()
    assert_connection_denied()

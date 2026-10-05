"""tests/plugins/network_guard.py keeps tests off the network without slowing them."""
import socket
import time

import pytest

from tests.plugins import network_guard


def test_a_made_up_hostname_fails_at_once_with_the_guards_message():
    started = time.monotonic()
    with pytest.raises(socket.gaierror, match="test network guard"):
        socket.getaddrinfo("nas", 80)
    assert time.monotonic() - started < 0.5


def test_an_address_literal_still_resolves():
    infos = socket.getaddrinfo("93.184.216.34", 443, proto=socket.IPPROTO_TCP)
    assert infos[0][4][0] == "93.184.216.34"


def test_a_connection_beyond_loopback_is_refused():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="test network guard"):
            sock.connect(("203.0.113.7", 80))
        assert sock.connect_ex(("203.0.113.7", 80)) != 0
    finally:
        sock.close()


def test_loopback_connections_work():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=2)
        client.close()
    finally:
        server.close()


@pytest.mark.allow_network
def test_the_marker_opens_the_guard_for_one_test():
    assert network_guard._allowed is True


def test_the_guard_is_closed_again_afterwards():
    assert network_guard._allowed is False

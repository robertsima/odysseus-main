"""Keep the default test run off the network.

A DNS lookup or a TCP connect to anything but loopback fails at once, with an
error that names this guard. Before it, tests that resolved made-up hostnames
("nas", "ep", "x") waited out the resolver: 10 s per lookup in WSL, 181 s in
one serial run. Tests that genuinely need the network carry
``@pytest.mark.allow_network``; that is rare and wants a reason in the test.

The guard is installed once per process in ``pytest_configure``, so module
imports during collection are covered as well.
"""
from __future__ import annotations

import errno
import ipaddress
import socket

import pytest

_REAL = {
    "getaddrinfo": socket.getaddrinfo,
    "gethostbyname": socket.gethostbyname,
    "gethostbyname_ex": socket.gethostbyname_ex,
    "gethostbyaddr": socket.gethostbyaddr,
    "connect": socket.socket.connect,
    "connect_ex": socket.socket.connect_ex,
}

_LOCAL_NAMES = {"", "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
_allowed = False


def _host_name(host) -> str:
    if host is None:
        return ""
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    return str(host).strip().lower().rstrip(".")


def _is_local(host) -> bool:
    name = _host_name(host)
    if name in _LOCAL_NAMES or name.endswith(".localhost"):
        return True
    if name == _host_name(socket.gethostname()):
        return True
    try:
        addr = ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return False
    return addr.is_loopback or addr.is_unspecified


def _blocked(what: str, host) -> str:
    return (f"blocked by the test network guard: {what} {host!r}. "
            "Fake the call, or mark the test @pytest.mark.allow_network with a reason.")


def _getaddrinfo(host, *args, **kwargs):
    if _allowed or _is_local(host):
        return _REAL["getaddrinfo"](host, *args, **kwargs)
    raise socket.gaierror(socket.EAI_NONAME, _blocked("DNS lookup of", host))


def _gethostbyname(host):
    if _allowed or _is_local(host):
        return _REAL["gethostbyname"](host)
    raise socket.gaierror(socket.EAI_NONAME, _blocked("DNS lookup of", host))


def _gethostbyname_ex(host):
    if _allowed or _is_local(host):
        return _REAL["gethostbyname_ex"](host)
    raise socket.gaierror(socket.EAI_NONAME, _blocked("DNS lookup of", host))


def _gethostbyaddr(host):
    if _allowed or _is_local(host):
        return _REAL["gethostbyaddr"](host)
    raise socket.herror(1, _blocked("reverse DNS lookup of", host))


def _remote_target(sock, address):
    if _allowed or sock.family not in (socket.AF_INET, socket.AF_INET6):
        return None
    host = address[0] if isinstance(address, tuple) and address else address
    return None if _is_local(host) else host


def _connect(self, address):
    host = _remote_target(self, address)
    if host is not None:
        raise OSError(errno.ENETUNREACH, _blocked("connection to", host))
    return _REAL["connect"](self, address)


def _connect_ex(self, address):
    if _remote_target(self, address) is not None:
        return errno.ENETUNREACH
    return _REAL["connect_ex"](self, address)


def pytest_configure(config):
    socket.getaddrinfo = _getaddrinfo
    socket.gethostbyname = _gethostbyname
    socket.gethostbyname_ex = _gethostbyname_ex
    socket.gethostbyaddr = _gethostbyaddr
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex


def pytest_unconfigure(config):
    socket.getaddrinfo = _REAL["getaddrinfo"]
    socket.gethostbyname = _REAL["gethostbyname"]
    socket.gethostbyname_ex = _REAL["gethostbyname_ex"]
    socket.gethostbyaddr = _REAL["gethostbyaddr"]
    socket.socket.connect = _REAL["connect"]
    socket.socket.connect_ex = _REAL["connect_ex"]


@pytest.fixture(autouse=True)
def _network_guard(request):
    """Open the network for the duration of a test marked ``allow_network``."""
    global _allowed
    if request.node.get_closest_marker("allow_network") is None:
        yield
        return
    _allowed = True
    try:
        yield
    finally:
        _allowed = False

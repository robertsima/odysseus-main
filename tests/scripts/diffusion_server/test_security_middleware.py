"""The diffusion server is not reachable from a browser tab by DNS rebinding.

scripts/diffusion_server.py binds 127.0.0.1 by default. It used to send
``allow_origins=["*"]``: an attacker page that resolved its own domain to
127.0.0.1 could read the API's replies and drive the GPU. The server now
refuses any Host header it was not told to serve and sends no CORS
permission unless the operator names an origin.

The real module is loaded with a stand-in for torch (not installed in CI);
nothing here touches a model.
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.security

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "diffusion_server.py"


@pytest.fixture
def server(monkeypatch):
    torch = types.ModuleType("torch")
    torch.bfloat16, torch.float16, torch.float32 = "bfloat16", "float16", "float32"
    monkeypatch.setitem(sys.modules, "torch", torch)
    # The module installs a fake xformers at import; register the names so
    # monkeypatch takes them out again afterwards.
    for name in ("xformers", "xformers.ops", "xformers.ops.fmha"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    spec = importlib.util.spec_from_file_location("diffusion_server_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _get(app, url, headers=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            return await client.get(url, headers=headers or {})

    return asyncio.run(run())


def test_the_server_answers_its_own_host_and_refuses_a_rebound_one(server):
    assert _get(server.app, "http://127.0.0.1/health").status_code == 200

    response = _get(server.app, "http://evil.example.com/health")

    assert response.status_code == 400


def test_by_default_no_other_origin_may_read_the_replies(server):
    response = _get(server.app, "http://127.0.0.1/health", headers={"Origin": "https://evil.example.com"})

    assert response.status_code == 200
    assert not response.headers.get("access-control-allow-origin")


def test_an_allowed_origin_does_not_open_the_server_to_others(server):
    app = FastAPI()
    app.get("/health")(lambda: {"status": "ok"})
    server._configure_security_middleware(
        app, server._compute_allowed_hosts("127.0.0.1"), server._compute_cors_origins(["http://localhost:7000"]))

    allowed = _get(app, "http://127.0.0.1/health", headers={"Origin": "http://localhost:7000"})
    other = _get(app, "http://127.0.0.1/health", headers={"Origin": "https://evil.example.com"})

    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:7000"
    assert other.headers.get("access-control-allow-origin") in (None, "")


def test_the_host_allowlist_is_the_bind_address_and_loopback_only(server):
    hosts = server._compute_allowed_hosts("0.0.0.0", extras=["", "  ", "localhost", "lan.example"])

    assert hosts == ["0.0.0.0", "127.0.0.1", "localhost", "::1", "lan.example"]


def test_reconfiguring_replaces_the_middleware_instead_of_stacking_it(server):
    app = FastAPI()
    hosts = server._compute_allowed_hosts("127.0.0.1")
    server._configure_security_middleware(app, hosts, [])
    server._configure_security_middleware(app, hosts, ["http://localhost:7000"])

    names = [m.cls.__name__ for m in app.user_middleware]

    assert names == ["CORSMiddleware", "TrustedHostMiddleware"]


def test_configuring_after_the_server_started_is_refused(server):
    app = FastAPI()
    server._configure_security_middleware(app, server._compute_allowed_hosts("127.0.0.1"), [])
    before = list(app.user_middleware)
    app.middleware_stack = app.build_middleware_stack()

    with pytest.raises(RuntimeError):
        server._configure_security_middleware(app, ["lan.example"], [])
    assert list(app.user_middleware) == before

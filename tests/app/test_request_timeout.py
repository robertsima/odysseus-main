"""The hard request timeout cuts slow routes but spares routes with their own timeout."""
import asyncio

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


@pytest.fixture
def client(api, monkeypatch):
    # The middleware is the app's own; `api` imports the app against a scratch
    # data folder. A tiny app carries it so the test controls the routes.
    app_module = api.module
    monkeypatch.setattr(app_module, "REQUEST_HARD_TIMEOUT", 0.05)

    async def slow(request):
        await asyncio.sleep(0.3)
        return PlainTextResponse("done")

    tiny = Starlette(routes=[
        Route("/api/memory/audit", slow),
        Route("/api/something-else", slow),
    ])
    tiny.add_middleware(app_module._RequestTimeoutMiddleware)
    return TestClient(tiny)


def test_ordinary_route_is_cut_off_at_the_hard_timeout(client):
    assert client.get("/api/something-else").status_code == 504


def test_memory_audit_keeps_its_own_llm_timeout(client):
    """The audit waits on an LLM for up to 120s; the 45s hard cut would kill every long audit."""
    response = client.get("/api/memory/audit")

    assert response.status_code == 200
    assert response.text == "done"

"""API discovery must stay bounded before results reach context truncation."""
import json

import pytest

from src.tools.system import do_app_api
from src.tool_execution import format_tool_result


@pytest.fixture
def api_schema(monkeypatch):
    import httpx
    import src.tool_implementations as impl

    paths = {
        f"/api/gallery/item-{i:03d}": {"get": {"summary": f"Inspect gallery item {i}"}}
        for i in range(120)
    }
    paths["/api/shell/exec"] = {"post": {"summary": "Execute shell"}}
    paths["/api/notes"] = {"get": {"summary": "Read private notes"}}

    class Response:
        status_code = 200

        def json(self):
            return {"paths": paths}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(impl, "_internal_headers", lambda **kwargs: {})


@pytest.mark.asyncio
async def test_discovery_is_paged_without_duplicate_endpoint_text(api_schema):
    first = await do_app_api('{"action":"endpoints"}')
    assert first["total"] == 120
    assert len(first["endpoints"]) == 25
    assert first["next_offset"] == 25
    rendered = format_tool_result("app_api", first)
    assert rendered.count("/api/gallery/item-000") == 1
    assert "/api/shell" not in rendered and "/api/notes" not in rendered

    second = await do_app_api(json.dumps({"action": "endpoints", "offset": first["next_offset"]}))
    assert second["endpoints"][0]["path"] == "/api/gallery/item-025"
    last = await do_app_api('{"action":"endpoints","offset":100}')
    assert len(last["endpoints"]) == 20
    assert last["next_offset"] is None


@pytest.mark.asyncio
async def test_discovery_filter_and_limit_are_applied_before_serializing(api_schema):
    filtered = await do_app_api('{"action":"endpoints","filter":"item-119"}')
    assert filtered["total"] == 1
    assert filtered["endpoints"][0]["path"].endswith("119")
    bounded = await do_app_api('{"action":"endpoints","limit":10000}')
    assert 0 < len(bounded["endpoints"]) <= 50
    assert bounded["next_offset"] == len(bounded["endpoints"])
    assert "(truncated" not in format_tool_result("app_api", bounded)


@pytest.mark.asyncio
async def test_malformed_pagination_is_a_tool_error():
    result = await do_app_api('{"action":"endpoints","limit":"many"}')
    assert result["exit_code"] == 1
    assert "must be integers" in result["error"]


async def test_launching_an_agent_through_the_raw_api_is_refused_with_the_real_tool():
    """The generic bridge must not be a second, unpoliced way to start workers.

    POSTing /api/agents/launch skips the chat's delegation policy, skips the
    loadout preflight, and (because the caller has to remember `parent_session`)
    orphans the worker so it never appears in the agent strip or in
    `manage_agent_loadout action=status`.
    """
    result = await do_app_api(
        json.dumps({"method": "POST", "path": "/api/agents/launch",
                    "body": {"task": "audit the repo", "profile": "Reader"}}),
        owner="alice",
    )

    assert result["exit_code"] == 1
    assert "manage_agent_loadout" in result["error"]
    assert "orchestrate_agents" in result["error"]

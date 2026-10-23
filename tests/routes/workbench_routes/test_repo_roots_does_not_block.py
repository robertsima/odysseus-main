"""A slow checkout must not stall the event loop (Workbench repository picker).

Production bug: ``/repo/roots`` ran discovery and two ``subprocess.run`` calls
per checkout on the event loop (a 1.95 s stall in the 2026-10 log), so every
unrelated request and agent stream waited behind it.
"""
import asyncio
import time

import httpx
import pytest

from routes import workbench_routes as wr


@pytest.fixture(autouse=True)
def _fresh_roots_answer():
    from src import poll_coalesce

    poll_coalesce._cache.clear()
    yield
    poll_coalesce._cache.clear()


def _client(api):
    cookies = dict(api.as_admin().cookies)
    transport = httpx.ASGITransport(app=api.app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies=cookies)


@pytest.fixture
def slow_checkouts(api, monkeypatch, tmp_path):
    calls = []
    repos = [{"path": str(tmp_path / f"r{i}"), "root": str(tmp_path), "branch": "main", "kind": "checkout"}
             for i in range(3)]
    for r in repos:
        (tmp_path / r["path"]).mkdir(parents=True, exist_ok=True)
        (tmp_path / r["path"] / ".git").mkdir()
    import src.agent_tools.claude_code_tools as cct
    from src import repo_inspect

    monkeypatch.setattr(cct, "discover_repositories", lambda limit=25: [dict(r) for r in repos][:limit])
    monkeypatch.setattr(repo_inspect, "allowed_roots", lambda: [str(tmp_path)])

    def slow(path):
        calls.append(path)
        time.sleep(0.4)  # blocking, like subprocess.run
        return 123.0

    monkeypatch.setattr(wr, "_checkout_activity", slow)
    return calls


async def test_slow_roots_does_not_hold_up_a_cheap_request(api, slow_checkouts):
    async with _client(api) as http:
        started = time.monotonic()
        finished = {}

        async def timed(name, url):
            r = await http.get(url)
            finished[name] = time.monotonic() - started
            return r

        roots, cheap = await asyncio.gather(
            timed("roots", "/api/workbench/repo/roots"),
            timed("cheap", "/api/workbench/runs?limit=1"),
        )
    assert roots.status_code == 200 and cheap.status_code == 200
    assert [r["activity_at"] for r in roots.json()["repositories"]] == [123.0] * 3
    assert finished["roots"] >= 1.0, "the fake checkouts were not slow"
    assert finished["cheap"] < finished["roots"] / 2


async def test_simultaneous_roots_requests_share_one_walk(api, slow_checkouts):
    async with _client(api) as http:
        answers = await asyncio.gather(*[http.get("/api/workbench/repo/roots") for _ in range(4)])
    assert all(a.status_code == 200 for a in answers)
    assert len(slow_checkouts) == 3


async def test_roots_stops_measuring_when_the_budget_is_spent(api, slow_checkouts, monkeypatch):
    monkeypatch.setattr(wr, "ROOTS_BUDGET_S", 0.5)
    async with _client(api) as http:
        body = (await http.get("/api/workbench/repo/roots")).json()
    assert len(slow_checkouts) == 2
    assert body["activity_unmeasured"] == 1
    assert [r["activity_at"] for r in body["repositories"]] == [123.0, 123.0, 0]


@pytest.mark.security
async def test_roots_still_confines_checkouts_to_the_approved_roots(api, monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    (outside / ".git").mkdir(parents=True)
    import src.agent_tools.claude_code_tools as cct
    from src import repo_inspect

    monkeypatch.setattr(cct, "discover_repositories",
                        lambda limit=25: [{"path": str(outside), "root": None, "branch": "x", "kind": "checkout"}])
    monkeypatch.setattr(repo_inspect, "allowed_roots", lambda: [str(tmp_path / "approved")])
    monkeypatch.setattr(wr, "_checkout_activity", lambda path: pytest.fail("measured an unapproved checkout"))
    async with _client(api) as http:
        body = (await http.get("/api/workbench/repo/roots")).json()
    assert body["repositories"][0]["activity_at"] == 0

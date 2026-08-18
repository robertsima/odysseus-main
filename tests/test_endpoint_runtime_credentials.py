"""Session-backed providers must get a freshly resolved token everywhere.

ChatGPT subscription / Copilot endpoints keep a refreshable credential in
ProviderAuthSession and leave ModelEndpoint.api_key empty. Code that read that
column directly sent a dead bearer (or none at all), so scheduled tasks 401'd
against a provider that interactive chat could still use.
"""

from types import SimpleNamespace

import pytest


class _Ep(SimpleNamespace):
    pass


def _subscription_endpoint():
    return _Ep(
        id="ep1",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key=None,                 # intentionally empty for this provider
        provider_auth_id="auth-123",
    )


def test_endpoint_runtime_headers_uses_refreshed_token(monkeypatch):
    import src.endpoint_resolver as er

    monkeypatch.setattr(
        er, "resolve_endpoint_runtime",
        lambda ep, owner=None: ("https://chatgpt.com/backend-api/codex", "fresh-token"),
    )
    headers = er.endpoint_runtime_headers(_subscription_endpoint(), owner="admin")
    assert "fresh-token" in repr(headers)


def test_endpoint_runtime_headers_falls_back_to_stored_key(monkeypatch):
    """A provider whose refresh is broken keeps its old behaviour."""
    import src.endpoint_resolver as er

    def _boom(ep, owner=None):
        raise RuntimeError("refresh unavailable")

    monkeypatch.setattr(er, "resolve_endpoint_runtime", _boom)
    ep = _Ep(id="ep2", base_url="https://api.openai.com/v1", api_key="sk-stored",
             provider_auth_id=None)
    headers = er.endpoint_runtime_headers(ep, owner="admin")
    assert headers.get("Authorization") == "Bearer sk-stored"


@pytest.mark.parametrize("owner", ["admin", None])
def test_scheduler_resolves_runtime_headers_for_tasks(monkeypatch, owner):
    """The task path must not build headers off the empty api_key column."""
    import core.database as cd
    import src.endpoint_resolver as er
    from src.task_scheduler import TaskScheduler

    ep = _subscription_endpoint()

    class _Query:
        def filter(self, *a, **kw):
            return self

        def all(self):
            return [ep]

    class _Db:
        def query(self, *a, **kw):
            return _Query()

        def close(self):
            pass

    monkeypatch.setattr(cd, "SessionLocal", lambda: _Db())
    monkeypatch.setattr("src.auth_helpers.owner_filter", lambda q, *a, **kw: q)

    seen = {}

    def _fake_runtime(endpoint, owner=None):
        seen["owner"] = owner
        seen["ep"] = endpoint
        return "https://chatgpt.com/backend-api/codex", "fresh-token"

    monkeypatch.setattr(er, "resolve_endpoint_runtime", _fake_runtime)

    scheduler = TaskScheduler.__new__(TaskScheduler)
    headers = scheduler._resolve_endpoint_headers(
        "https://chatgpt.com/backend-api/codex/responses", owner
    )

    assert seen["ep"] is ep, "the matching endpoint row was never resolved"
    assert seen["owner"] == owner
    assert "fresh-token" in repr(headers)
    # The stale column must not be what gets sent.
    assert "None" not in repr(headers.get("Authorization", ""))


def test_agent_loop_fallback_forwards_resolved_headers(monkeypatch):
    """The simple-call fallback must carry auth, not dispatch bare.

    When the agent loop dies mid-run (a 502 from an overloaded provider is the
    common case), _execute_llm_task retries through task_llm_call_async. With no
    task/utility endpoint configured, resolve_endpoint hands the caller's
    (url, model, headers) straight back — so omitting headers there sent the
    retry with no Authorization at all and the run died on a 401 that looked
    like an expired subscription.
    """
    import asyncio

    import src.task_endpoint as te
    from src.task_scheduler import TaskScheduler

    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._session_manager = None
    scheduler._last_run_model = None

    async def _boom(*a, **kw):
        raise RuntimeError("Our servers are currently overloaded. (HTTP 502)")

    monkeypatch.setattr(TaskScheduler, "_run_agent_loop", _boom, raising=False)
    monkeypatch.setattr(
        TaskScheduler, "_resolve_endpoint_headers",
        lambda self, url, owner, db=None: {"Authorization": "Bearer fresh-token"},
        raising=False,
    )

    seen = {}

    class _Stop(Exception):
        pass

    async def _fake_call(messages, **kwargs):
        seen.update(kwargs)
        raise _Stop()

    monkeypatch.setattr(te, "task_llm_call_async", _fake_call)

    task = SimpleNamespace(
        id="t1", name="Daily Planning Forecast", prompt="plan the day",
        endpoint_url="https://chatgpt.com/backend-api/codex/responses",
        model="gpt-5.5", owner="admin", session_id="sess-1",
        crew_member_id=None, character_id=None,
    )

    with pytest.raises(_Stop):
        asyncio.run(scheduler._execute_llm_task(task, db=None))

    assert seen.get("fallback_headers") == {"Authorization": "Bearer fresh-token"}, (
        "the fallback call dispatched without the endpoint's auth headers"
    )

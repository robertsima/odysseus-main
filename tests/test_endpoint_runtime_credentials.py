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

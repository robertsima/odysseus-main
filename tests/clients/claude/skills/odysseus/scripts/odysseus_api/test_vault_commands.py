"""The bundled Claude Code helper's vault commands reach the real vault routes.

The helper is what an agent session runs, so a vault route renamed without
the helper (or the other way round) leaves the agent with a command that
always fails.
"""
import importlib.util
import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.rag_singleton
from src import ai_interaction

HELPER = Path(__file__).resolve().parents[7] / "clients/claude/skills/odysseus/scripts/odysseus_api.py"


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location("odysseus_api_helper", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def run_helper(api, helper, monkeypatch, capsys):
    """Run the helper's main() with its HTTP requests served by the real app."""
    token = api.as_admin().post("/api/tokens", data={"name": "agent", "scopes": "vault:read"}).json()["token"]
    monkeypatch.setenv("ODYSSEUS_URL", "http://odysseus.test")
    monkeypatch.setenv("ODYSSEUS_API_TOKEN", token)
    client = api.anonymous()

    def urlopen(req, timeout=None):
        url = req.full_url.removeprefix("http://odysseus.test")
        response = client.request(req.get_method(), url, headers=dict(req.header_items()))
        if response.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, response.status_code, "", {}, io.BytesIO(response.content))
        return io.BytesIO(response.content)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    def run(*args):
        monkeypatch.setattr("sys.argv", ["odysseus_api.py", *args])
        code = helper.main()
        out, err = capsys.readouterr()
        return code, out, err

    return run


def test_vault_search_returns_the_routes_results(run_helper, monkeypatch):
    searches = []

    def search(query, k, allow_private):
        searches.append((query, k))
        return [{"document": "the plan", "similarity": 0.8, "metadata": {"file_path": "/vault/plan.md"}}]

    monkeypatch.setattr(src.rag_singleton, "get_rag_manager", lambda: SimpleNamespace(healthy=True, search=search))

    code, out, err = run_helper("vault", "search", "my plans", "3")

    assert code == 0, err
    assert json.loads(out)["results"][0]["excerpt"] == "the plan"
    assert searches == [("my plans", 3)]


def test_vault_read_returns_the_note(run_helper, tmp_path, monkeypatch):
    note = tmp_path / "plan.md"
    note.write_text("the plan", encoding="utf-8")
    manager = SimpleNamespace(index=[{"path": str(note), "sensitivity": "public"}])
    monkeypatch.setattr(ai_interaction, "_personal_docs_manager", manager, raising=False)

    code, out, err = run_helper("vault", "read", str(note))

    assert code == 0, err
    assert json.loads(out)["content"] == "the plan"

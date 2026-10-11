"""A workspace passed on a resume is saved on the chat, so the next resume needs no repair.

Bug (2026-10-10): a person fixed "Pass workspace=..." by hand on one
send_to_session call; the chat kept its stale path and the next resume failed
the same way.
"""
import json
import os

import pytest

import src.database
from core.database import get_session_settings, update_session_settings
from src.agent_tools import session_tools


@pytest.fixture
def chats(api, monkeypatch):
    """A lead chat and a worker chat for `owner`, with the agent loop answering at once."""
    import src.agent_loop as agent_loop

    async def model(url, model_id, messages, **kwargs):
        yield 'data: {"delta": "Resumed."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(session_tools, "get_session_manager", lambda: api.session_manager)
    monkeypatch.setattr(agent_loop, "stream_agent_loop", model)

    def make(client, owner):
        db = src.database.SessionLocal()
        try:
            db.add(src.database.ModelEndpoint(
                id=f"ep-{owner}", name="Test model", base_url="http://model.test/v1", is_enabled=True,
                owner=owner, cached_models=json.dumps(["m"]),
            ))
            db.commit()
        finally:
            db.close()
        ids = {}
        for name in ("Lead", "Worker"):
            created = client.post("/api/session", data={
                "name": name, "skip_validation": "true", "endpoint_id": f"ep-{owner}", "model": "m"})
            assert created.status_code == 200, created.text
            ids[name] = created.json()["id"]
        return ids

    return make


async def _resume(ids, owner, **extra):
    return await session_tools.send_to_session(
        json.dumps({"session_id": ids["Worker"], "message": "please finish the docs", "mode": "agent", **extra}),
        ids["Lead"], owner=owner)


@pytest.mark.asyncio
async def test_an_explicit_workspace_on_a_resume_replaces_a_stale_saved_one(api, chats, tmp_path):
    ids = chats(api.as_admin(), "admin")
    folder = tmp_path / "repo"
    folder.mkdir()
    update_session_settings(ids["Worker"], {"workspace": str(tmp_path / "deleted-worktree")})

    result = await _resume(ids, "admin", workspace=str(folder))

    assert not result.get("error"), result
    saved = get_session_settings(ids["Worker"])["workspace"]
    assert os.path.normcase(saved) == os.path.normcase(os.path.realpath(folder))


@pytest.mark.asyncio
async def test_a_blocked_resume_names_the_stale_saved_path(api, chats, tmp_path):
    ids = chats(api.as_admin(), "admin")
    stale = str(tmp_path / "deleted-worktree")
    update_session_settings(ids["Worker"], {"workspace": stale})

    result = await session_tools.send_to_session(
        json.dumps({"session_id": ids["Worker"], "message": "Fix the bug in src/app.py", "mode": "agent",
                    "requires": ["workspace"]}),
        ids["Lead"], owner="admin")

    assert result["status"] == "blocked"
    assert stale in result["error"] and "this chat has none" not in result["error"]


@pytest.mark.security
@pytest.mark.asyncio
async def test_a_regular_users_resume_does_not_save_a_workspace(api, chats, tmp_path):
    ids = chats(api.as_user("alice"), "alice")
    folder = tmp_path / "repo"
    folder.mkdir()

    await _resume(ids, "alice", workspace=str(folder))

    assert not get_session_settings(ids["Worker"]).get("workspace")

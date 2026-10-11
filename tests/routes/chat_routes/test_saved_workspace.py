"""A chat's saved workspace outlives turns that carry none.

Bug (2026-10-10): every foreground turn wrote `workspace or None` into the chat
settings, so one turn from a client that sent no workspace erased the folder the
chat belonged to, and a headless resume then failed with "this chat has none".
"""
import os

import pytest

from tests.routes.chat_routes.agent_turn import agent_turn  # noqa: F401  (fixture)


def _saved(client, chat):
    return client.get(f"/api/session/{chat}/settings").json()["settings"].get("workspace")


def test_a_turn_without_a_workspace_keeps_the_saved_one(agent_turn, tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    assert agent_turn.client.patch(
        f"/api/session/{agent_turn.session_id}/settings", json={"workspace": str(folder)}
    ).status_code == 200

    agent_turn.send({"message": "list the files here", "mode": "agent"})

    assert os.path.normcase(_saved(agent_turn.client, agent_turn.session_id)) == os.path.normcase(os.path.realpath(folder))


def test_a_turn_without_a_workspace_runs_in_the_saved_one(agent_turn, tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    agent_turn.client.patch(f"/api/session/{agent_turn.session_id}/settings", json={"workspace": str(folder)})

    turn = agent_turn.send({"message": "list the files here", "mode": "agent"})

    assert os.path.normcase(turn.loop_kwargs["workspace"]) == os.path.normcase(os.path.realpath(folder))


def test_a_vetted_workspace_on_a_turn_replaces_the_saved_one(agent_turn, tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    agent_turn.client.patch(f"/api/session/{agent_turn.session_id}/settings", json={"workspace": str(first)})

    agent_turn.send({"message": "list the files here", "mode": "agent", "workspace": str(second)})

    assert os.path.normcase(_saved(agent_turn.client, agent_turn.session_id)) == os.path.normcase(os.path.realpath(second))


def test_a_deleted_saved_folder_is_not_bound_and_not_erased(agent_turn, tmp_path):
    """The stale path stays visible to the person and to preflight; the turn runs unconfined."""
    folder = tmp_path / "gone"
    folder.mkdir()
    agent_turn.client.patch(f"/api/session/{agent_turn.session_id}/settings", json={"workspace": str(folder)})
    folder.rmdir()

    turn = agent_turn.send({"message": "list the files here", "mode": "agent"})

    assert turn.loop_kwargs["workspace"] is None
    assert _saved(agent_turn.client, agent_turn.session_id)


def test_clearing_stays_with_the_settings_patch(agent_turn, tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    agent_turn.client.patch(f"/api/session/{agent_turn.session_id}/settings", json={"workspace": str(folder)})
    agent_turn.client.patch(f"/api/session/{agent_turn.session_id}/settings", json={"workspace": None})

    turn = agent_turn.send({"message": "list the files here", "mode": "agent"})

    assert turn.loop_kwargs["workspace"] is None
    assert not _saved(agent_turn.client, agent_turn.session_id)


@pytest.mark.security
def test_a_regular_user_turn_never_gets_a_workspace(api, monkeypatch, tmp_path):
    """A non-admin chat holding a workspace in its settings must not run confined to it."""
    import json
    import src.database
    from core.database import update_session_settings
    from routes import chat_routes
    from src import model_context
    from tests.routes.chat_routes.agent_turn import REPLY

    db = src.database.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id="alice-ep", name="alice endpoint", base_url="http://127.0.0.1:9/v1", is_enabled=True,
            owner="alice", cached_models=json.dumps(["test-model"]),
        ))
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(model_context, "_query_context_length", lambda endpoint_url, model: (32768, True))
    calls = []

    async def scripted_loop(*args, **kwargs):
        calls.append(kwargs)
        for chunk in REPLY:
            yield chunk

    monkeypatch.setattr(chat_routes, "stream_agent_loop", scripted_loop)
    alice = api.as_user("alice")
    chat = alice.post("/api/session", data={
        "name": "mine", "endpoint_id": "alice-ep", "model": "test-model", "skip_validation": "true",
    }).json()["id"]
    folder = tmp_path / "repo"
    folder.mkdir()
    update_session_settings(chat, {"workspace": str(folder)})

    response = alice.post("/api/chat_stream", data={
        "session": chat, "message": "list the files here", "mode": "agent", "workspace": str(folder),
    })

    assert response.status_code == 200, response.text
    assert calls and calls[-1]["workspace"] is None

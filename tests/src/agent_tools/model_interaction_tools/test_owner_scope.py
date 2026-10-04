"""list_models shows a user only the endpoints they may use."""
import uuid

import pytest

import src.database
from src.agent_tools import model_interaction_tools

pytestmark = pytest.mark.security


def _add_endpoint(app_db, owner, name):
    db = app_db.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id=str(uuid.uuid4()), name=name, base_url="https://api.anthropic.com/v1",
            is_enabled=True, owner=owner,
        ))
        db.commit()
    finally:
        db.close()


async def test_list_models_leaves_out_another_users_endpoint(app_db, monkeypatch):
    monkeypatch.setattr(src.database, "SessionLocal", app_db.SessionLocal)
    _add_endpoint(app_db, "alice", "alice-anthropic")
    _add_endpoint(app_db, "bob", "bob-anthropic")

    alice_view = (await model_interaction_tools.list_models("", owner="alice"))["results"]
    bob_view = (await model_interaction_tools.list_models("", owner="bob"))["results"]

    assert "alice-anthropic" in alice_view and "bob-anthropic" not in alice_view
    assert "bob-anthropic" in bob_view and "alice-anthropic" not in bob_view

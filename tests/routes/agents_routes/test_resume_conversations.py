"""Opening a persistent agent must navigate, not proliferate conversations."""
import pytest

from .test_persistent_conversations import phalanx, plain_chat, talk


def open_agent(api, owner="alice", **body):
    response = api.as_user(owner).post("/api/agents/profiles/Reader/chat", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_first_visit_creates_then_reopens_the_same_empty_chat(phalanx):
    donor = plain_chat(phalanx, "alice")
    first = open_agent(phalanx, from_session=donor)["session_id"]
    assert open_agent(phalanx, from_session=donor)["session_id"] == first
    phalanx.session_manager.sessions.clear()
    phalanx.session_manager.load_sessions()
    assert open_agent(phalanx)["session_id"] == first


def test_explicit_new_chat_is_separate_and_current_conversation_wins(phalanx):
    first = talk(phalanx)
    second = open_agent(phalanx, from_session=first, new_chat=True)["session_id"]
    assert second != first
    assert open_agent(phalanx, from_session=first)["session_id"] == first
    assert open_agent(phalanx)["session_id"] == first
    assert open_agent(phalanx, from_session=second)["session_id"] == second
    assert open_agent(phalanx)["session_id"] == second


@pytest.mark.security
def test_resume_never_reopens_archived_or_other_owners_chat(phalanx):
    alice = talk(phalanx)
    bob = talk(phalanx, "bob")
    assert phalanx.as_user("alice").post(f"/api/agents/sessions/{alice}/archive").status_code == 200
    donor = plain_chat(phalanx, "alice")
    created = open_agent(phalanx, from_session=donor)["session_id"]
    assert created not in {alice, bob}
    assert open_agent(phalanx, "bob", from_session=bob)["session_id"] == bob
    assert phalanx.as_user("bob").post("/api/agents/profiles/Reader/chat", json={"from_session": created}).status_code == 404


def test_delegated_worker_is_not_resumed_as_a_conversation(phalanx):
    from core.database import update_session_settings
    parent = plain_chat(phalanx, "alice")
    worker = plain_chat(phalanx, "alice")
    update_session_settings(worker, {"parent_session": parent, "agent_profile": "Reader"})
    assert open_agent(phalanx, from_session=parent)["session_id"] != worker


def test_simultaneous_first_opens_share_one_conversation(phalanx, monkeypatch):
    """Two clicks while a model resolves must not create two empty chats."""
    import asyncio
    import time
    import httpx
    from src import agent_profiles, ai_interaction
    profiles = agent_profiles.validate_profiles([{"name": "Reader", "model": "m", "tool_access": "all"}])
    monkeypatch.setattr(agent_profiles, "load_profiles", lambda: profiles)

    def resolve(*args):
        time.sleep(0.05)
        return ("http://127.0.0.1:9/v1", "m", None)

    monkeypatch.setattr(ai_interaction, "_resolve_model", resolve)
    cookies = dict(phalanx.as_user("alice").cookies)

    async def opens():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=phalanx.module.app),
                                     base_url="http://testserver", cookies=cookies) as client:
            responses = await asyncio.gather(*[
                client.post("/api/agents/profiles/Reader/chat", json={}) for _ in range(2)])
            assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
            return [r.json()["session_id"] for r in responses]

    first, second = asyncio.run(opens())
    assert first == second

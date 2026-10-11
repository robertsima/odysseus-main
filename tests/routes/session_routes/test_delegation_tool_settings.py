"""Switching a delegation tool on in a chat's tool settings is remembered as consent.

Bug (2026-10-10): the person switched `manage_agent_loadout` on for the chat and
the next turn still had it denied, because the per-message delegation gate never
looked at the tool settings. The settings route now records the switch; the loop
reads it (tests/src/agent_loop/test_delegation_consent.py).
"""
import pytest

from core.middleware import INTERNAL_TOOL_HEADER


def _chat(client):
    return client.post("/api/session", data={"name": "lead", "skip_validation": "true"}).json()["id"]


def _enabled(client, chat):
    return client.get(f"/api/session/{chat}/settings").json()["settings"].get("delegation_tools_enabled")


def test_switching_a_delegation_tool_on_is_recorded(api):
    alice = api.as_user("alice")
    chat = _chat(alice)
    alice.patch(f"/api/session/{chat}/settings", json={"disabled_tools": ["manage_agent_loadout", "bash"]})

    alice.patch(f"/api/session/{chat}/settings", json={"disabled_tools": ["bash"]})

    assert _enabled(alice, chat) == ["manage_agent_loadout"]


def test_all_tools_on_records_every_delegation_tool_that_was_off(api):
    alice = api.as_user("alice")
    chat = _chat(alice)
    alice.patch(f"/api/session/{chat}/settings", json={"disabled_tools": ["send_to_session"]})

    alice.patch(f"/api/session/{chat}/settings", json={"disabled_tools": None})

    assert _enabled(alice, chat) == ["send_to_session"]


def test_switching_it_off_again_withdraws_the_consent(api):
    alice = api.as_user("alice")
    chat = _chat(alice)
    alice.patch(f"/api/session/{chat}/settings", json={"disabled_tools": ["send_to_session"]})
    alice.patch(f"/api/session/{chat}/settings", json={"disabled_tools": []})

    alice.patch(f"/api/session/{chat}/settings", json={"disabled_tools": ["send_to_session"]})

    assert not _enabled(alice, chat)


@pytest.mark.security
def test_an_agent_cannot_grant_itself_consent_through_the_settings_api(api):
    """The loopback tool header marks a call the model made, not the person."""
    alice = api.as_user("alice")
    chat = _chat(alice)
    alice.patch(f"/api/session/{chat}/settings", json={"disabled_tools": ["manage_agent_loadout"]})

    response = alice.patch(f"/api/session/{chat}/settings", json={"disabled_tools": []},
                           headers={INTERNAL_TOOL_HEADER: "agent"})

    assert response.status_code == 200, response.text  # the change is saved, only the consent is not
    assert alice.get(f"/api/session/{chat}/settings").json()["settings"].get("disabled_tools") in (None, [])
    assert not _enabled(alice, chat)


@pytest.mark.security
def test_the_consent_key_cannot_be_written_directly(api):
    alice = api.as_user("alice")
    chat = _chat(alice)

    response = alice.patch(f"/api/session/{chat}/settings", json={"delegation_tools_enabled": ["send_to_session"]})

    assert response.status_code == 400

"""Editing a loadout in Settings reaches the chats already running under it.

A chat switched to a loadout holds a copy of its policy, so on 2026-09-24
setting Delegation to "Agent decides" in Settings changed nothing for the chat
using that loadout; only the Control Room (which edits the chat's copy) did.
"""

import json
import uuid

from core.database import Session, get_db_session, get_session_settings, init_db
from src.agent_profiles import propagate_profile_edits, session_patch, validate_profiles


def _chat(settings):
    init_db()
    sid = f"prop-{uuid.uuid4().hex[:8]}"
    with get_db_session() as db:
        db.add(Session(id=sid, name="chat", endpoint_url="http://local", model="m",
                       settings_json=json.dumps(settings)))
    return sid


def _profiles(**kw):
    return validate_profiles([{"name": "Odysseus Admin", "tool_access": "selected",
                               "enabled_tools": ["read_file", "web_search"], **kw}])


def test_a_settings_edit_reaches_a_chat_using_the_loadout():
    old = _profiles(delegation_policy="explicit")
    new = _profiles(delegation_policy="auto")
    chat = _chat(session_patch(old[0]))

    assert propagate_profile_edits(old, new) == {"Odysseus Admin": 1}
    assert get_session_settings(chat)["delegation_policy"] == "auto"


def test_a_value_changed_on_the_chat_itself_is_kept():
    old = _profiles(delegation_policy="explicit", memory_access="read")
    new = _profiles(delegation_policy="auto", memory_access="write")
    # Someone set this chat to "never" in the Control Room.
    chat = _chat({**session_patch(old[0]), "delegation_policy": "never"})

    propagate_profile_edits(old, new)
    settings = get_session_settings(chat)
    assert settings["delegation_policy"] == "never"
    assert settings["memory_access"] == "write"


def test_workers_other_loadouts_and_the_persona_are_left_alone():
    old = _profiles(delegation_policy="explicit", instructions="old voice")
    new = _profiles(delegation_policy="auto", instructions="new voice")
    worker = _chat({**session_patch(old[0]), "parent_session": "p"})
    other = _chat({**session_patch(old[0]), "agent_profile": "Someone Else"})
    mine = _chat(session_patch(old[0]))

    propagate_profile_edits(old, new)
    assert get_session_settings(worker)["delegation_policy"] == "explicit"
    assert get_session_settings(other)["delegation_policy"] == "explicit"
    assert get_session_settings(mine)["agent_instructions"] == "old voice"
    assert get_session_settings(mine)["delegation_policy"] == "auto"

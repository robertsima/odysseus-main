"""Delegation consent belongs to the chat, not to one message.

Bug (2026-10-10): the person asked a chat to have its Lead Engineer worker build
a feature. Every later turn ("please just finish this") re-ran the per-message
`explicit` gate, found no hand-off wording, and denied `send_to_session` and
`manage_agent_loadout`, so the existing worker could not be resumed without the
person retyping the magic wording. Switching the tool on in the chat's tool
settings changed nothing either.
"""
import json
import uuid

import pytest

import core.database as cdb
from core.database import Session as DbSession
from src import delegation_consent
from src.agent_loop import _DELEGATION_TOOLS, _delegation_gate

ASK = "ask the Lead Engineer worker to implement the export feature"
FINISH = "please just finish this"
LAUNCHERS = set(_DELEGATION_TOOLS) - delegation_consent.CONTINUE_TOOLS


@pytest.fixture
def db(monkeypatch, app_db):
    monkeypatch.setattr(cdb, "SessionLocal", app_db.SessionLocal)
    return app_db


def _chat(db, parent=None, owner="alice"):
    sid = str(uuid.uuid4())
    session = db.SessionLocal()
    session.add(DbSession(id=sid, owner=owner, name="chat", endpoint_url="http://x", model="m", archived=False,
                          settings_json=json.dumps({"parent_session": parent}) if parent else None))
    session.commit()
    session.close()
    return sid


def _gate(sid, text, policy="explicit"):
    return _delegation_gate(policy, text, sid, cdb.get_session_settings(sid))


def test_an_explicit_request_carries_to_later_turns_for_the_chats_own_workers(db):
    chat = _chat(db)
    assert _gate(chat, ASK).gated == set()

    later = _gate(chat, FINISH)

    assert later.gated == LAUNCHERS
    assert later.continue_only == delegation_consent.CONTINUE_TOOLS


def test_a_chat_that_never_asked_stays_fully_gated(db):
    chat = _chat(db)

    gate = _gate(chat, FINISH)

    assert gate.gated == set(_DELEGATION_TOOLS)
    assert not gate.continue_only


def test_a_chat_with_child_workers_may_resume_them_without_new_wording(db):
    chat = _chat(db)
    _chat(db, parent=chat)

    gate = _gate(chat, FINISH)

    assert gate.gated == LAUNCHERS
    assert gate.continue_only == delegation_consent.CONTINUE_TOOLS


def test_never_stays_absolute_even_with_a_grant_and_children(db):
    chat = _chat(db)
    _gate(chat, ASK)
    _chat(db, parent=chat)

    gate = _gate(chat, FINISH, policy="never")

    assert gate.gated == set(_DELEGATION_TOOLS)
    assert not gate.continue_only


def test_a_prohibition_takes_the_consent_away_even_with_children(db):
    chat = _chat(db)
    _gate(chat, ASK)
    _chat(db, parent=chat)

    gate = _gate(chat, "do not use any agents or workers for this")

    assert gate.gated == set(_DELEGATION_TOOLS)
    assert cdb.get_session_settings(chat)["delegation_granted"] is False


def test_a_worker_hand_back_never_grants_consent(db):
    chat = _chat(db)

    _delegation_gate("explicit", ASK, chat, cdb.get_session_settings(chat), from_worker=True)

    assert not cdb.get_session_settings(chat).get("delegation_granted")


def test_a_tool_switched_on_in_the_chats_settings_opens_that_tool_fully(db):
    chat = _chat(db)
    delegation_consent.record_tool_settings_change(
        chat, {"disabled_tools": ["manage_agent_loadout", "bash"]}, {"disabled_tools": ["bash"]})

    gate = _gate(chat, FINISH)

    assert "manage_agent_loadout" not in gate.gated
    assert "manage_agent_loadout" not in gate.continue_only
    assert gate.gated == set(_DELEGATION_TOOLS) - {"manage_agent_loadout"}


def test_switching_the_tool_off_again_withdraws_that_consent(db):
    chat = _chat(db)
    delegation_consent.record_tool_settings_change(chat, {"disabled_tools": ["manage_agent_loadout"]}, {})
    delegation_consent.record_tool_settings_change(chat, {}, {"disabled_tools": ["manage_agent_loadout"]})

    assert "manage_agent_loadout" in _gate(chat, FINISH).gated


# ── what a continue-only turn lets through ───────────────────────────────

def _send(target, message="finish"):
    return json.dumps({"session_id": target, "message": message})


def test_resuming_the_chats_own_worker_passes(db):
    chat = _chat(db)
    worker = _chat(db, parent=chat)

    assert delegation_consent.continuation_refusal("send_to_session", _send(worker), chat, "alice") is None


@pytest.mark.parametrize("make_target", ["new", "someone_elses", "unrelated"])
def test_starting_a_new_worker_or_messaging_a_stranger_is_refused(db, make_target):
    chat = _chat(db)
    other_parent = _chat(db)
    target = {"new": "new", "someone_elses": _chat(db, parent=other_parent), "unrelated": _chat(db)}[make_target]

    refusal = delegation_consent.continuation_refusal("send_to_session", _send(target), chat, "alice")

    assert refusal and "delegation policy" in refusal


@pytest.mark.parametrize("action,allowed", [
    ("status", True), ("poll", True), ("stop", True),
    ("start", False), ("create", False), ("update", False), ("delete", False), ("import", False),
])
def test_manage_agent_loadout_continues_but_does_not_start(db, action, allowed):
    chat = _chat(db)

    refusal = delegation_consent.continuation_refusal(
        "manage_agent_loadout", json.dumps({"action": action}), chat, "alice")

    assert (refusal is None) is allowed


# ── allowlist chats ──────────────────────────────────────────────────────

_SELECTED = {"tool_access": "selected", "enabled_tools": ["web_search"]}


def test_adding_a_delegation_tool_to_a_selected_allowlist_opens_it(db):
    chat = _chat(db)
    after = {**_SELECTED, "enabled_tools": ["web_search", "send_to_session"]}
    delegation_consent.record_tool_settings_change(chat, _SELECTED, after)

    assert "send_to_session" not in _gate(chat, FINISH).gated


def test_saving_an_allowlist_that_already_had_the_tool_is_not_a_switch(db):
    chat = _chat(db)
    both = {**_SELECTED, "enabled_tools": ["send_to_session"]}
    delegation_consent.record_tool_settings_change(chat, both, {**both, "enabled_tools": ["send_to_session", "grep"]})

    assert "send_to_session" in _gate(chat, FINISH).gated


def test_removing_a_tool_from_the_allowlist_withdraws_it(db):
    chat = _chat(db)
    on = {**_SELECTED, "enabled_tools": ["send_to_session"]}
    delegation_consent.record_tool_settings_change(chat, _SELECTED, on)
    delegation_consent.record_tool_settings_change(chat, on, _SELECTED)

    assert "send_to_session" in _gate(chat, FINISH).gated

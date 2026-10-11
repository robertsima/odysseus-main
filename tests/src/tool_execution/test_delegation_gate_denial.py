"""A delegation tool the policy holds back says so, and a continue-only turn cannot start work.

Bug (2026-10-10): the model was told "Tool 'manage_agent_loadout' is disabled by
user", which read like a broken permission, so it and the person looked for a
switch that did nothing. The gate it hit is the chat's delegation policy.
"""
import asyncio
import json
import uuid

import pytest

import core.database as cdb
import src.tool_execution as te
from core.database import Session as DbSession
from src import delegation_consent
from src.agent_tools import ToolBlock


@pytest.fixture
def env(monkeypatch, app_db):
    monkeypatch.setattr(cdb, "SessionLocal", app_db.SessionLocal)
    monkeypatch.setattr("src.settings.load_disabled_tools_strict", lambda: [])
    monkeypatch.setattr("src.tool_security.owner_baseline_disabled_tools", lambda owner: set())
    return app_db


def _chat(db, parent=None):
    sid = str(uuid.uuid4())
    session = db.SessionLocal()
    session.add(DbSession(id=sid, owner="alice", name="worker", endpoint_url="http://x", model="m", archived=False,
                          settings_json=json.dumps({"parent_session": parent}) if parent else None))
    session.commit()
    session.close()
    return sid


def _run(tool, content, chat, scope, disabled=()):
    return asyncio.run(te.execute_tool_block(
        ToolBlock(tool, content), session_id=chat, owner="alice", disabled_tools=set(disabled),
        delegation_gate=scope, security_context=te.NO_TOOL_SECURITY_CONTEXT))[1]


def test_a_gated_tool_names_the_delegation_policy_and_what_unlocks_it(env):
    chat = _chat(env)
    scope = {"policy": "explicit", "gated": frozenset({"manage_agent_loadout"}), "continue_only": frozenset()}

    result = _run("manage_agent_loadout", '{"action": "status"}', chat, scope, disabled={"manage_agent_loadout"})

    assert result["blocked_reason"] == "delegation_policy"
    assert "delegation policy" in result["error"] and "disabled by user" not in result["error"]
    assert "tool settings" in result["error"] and "ask" in result["error"].lower()


def test_never_says_only_the_person_can_change_it(env):
    chat = _chat(env)
    scope = {"policy": "never", "gated": frozenset({"send_to_session"}), "continue_only": frozenset()}

    result = _run("send_to_session", '{"session_id": "new", "message": "x"}', chat, scope, disabled={"send_to_session"})

    assert "'never'" in result["error"] and "Only the person" in result["error"]


def test_a_tool_the_person_disabled_keeps_the_plain_message(env):
    chat = _chat(env)
    scope = {"policy": "explicit", "gated": frozenset(), "continue_only": frozenset()}

    result = _run("send_to_session", '{"session_id": "new", "message": "x"}', chat, scope, disabled={"send_to_session"})

    assert "disabled by user" in result["error"]


@pytest.mark.security
def test_a_continue_only_turn_cannot_start_a_new_worker(env):
    chat = _chat(env)
    scope = {"policy": "explicit", "gated": frozenset(), "continue_only": delegation_consent.CONTINUE_TOOLS}

    new = _run("send_to_session", '{"session_id": "new", "message": "build it", "mode": "agent"}', chat, scope)
    start = _run("manage_agent_loadout", '{"action": "start", "name": "Lead Engineer", "task": "x"}', chat, scope)

    assert new["blocked_reason"] == "delegation_policy"
    assert start["blocked_reason"] == "delegation_policy"


def test_a_continue_only_turn_reaches_the_tool_for_the_chats_own_worker(env):
    chat = _chat(env)
    worker = _chat(env, parent=chat)
    scope = {"policy": "explicit", "gated": frozenset(), "continue_only": delegation_consent.CONTINUE_TOOLS}

    result = _run("send_to_session", json.dumps({"session_id": worker, "message": "finish it"}), chat, scope)

    assert result.get("blocked_reason") != "delegation_policy"

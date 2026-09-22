"""Each agent loadout keeps its own persona, instructions and sampling.

Personas in the Prompt window are shared; a chat running as an agent under a
loadout must not pick them up, and two loadouts must not share a voice.
"""
import asyncio
from pathlib import Path

import pytest

import core.database as database
import src.agent_loop as agent_loop
from routes import chat_helpers
from src import agent_profiles, session_settings

ROOT = Path(__file__).resolve().parent.parent


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]
    return asyncio.run(_run())


def test_profiles_carry_their_own_persona_and_sampling():
    scout, plain = agent_profiles.validate_profiles([
        {"name": "Scout", "persona_name": "Scout", "instructions": "Terse field notes.",
         "temperature": "0.3", "max_tokens": 900},
        {"name": "Plain"},
    ])
    assert (scout["persona_name"], scout["temperature"], scout["max_tokens"]) == ("Scout", 0.3, 900)
    # Blank means "app default", not zero.
    assert (plain["persona_name"], plain["temperature"], plain["max_tokens"]) == ("", None, None)
    assert agent_profiles.validate_profiles([{"name": "Hot", "temperature": 9}])[0]["temperature"] == 2.0
    with pytest.raises(ValueError):
        agent_profiles.validate_profiles([{"name": "Bad", "temperature": "warm"}])

    patch = agent_profiles.session_patch(scout)
    assert patch["agent_persona_name"] == "Scout"
    assert patch["agent_instructions"] == "Terse field notes."
    assert (patch["agent_temperature"], patch["agent_max_tokens"]) == (0.3, 900)
    # Every key is one the chat settings accept.
    session_settings.validate_patch(patch)


def test_chat_settings_accept_and_bound_the_voice_keys():
    out = session_settings.validate_patch({
        "agent_persona_name": "  Scout  ", "agent_temperature": 5, "agent_max_tokens": "", })
    assert out == {"agent_persona_name": "Scout", "agent_temperature": 2.0, "agent_max_tokens": None}
    with pytest.raises(ValueError):
        session_settings.validate_patch({"agent_temperature": True})

    assert session_settings.loadout_voice({"toggles": {"mode": "agent"}}) is None
    voice = session_settings.loadout_voice({"agent_profile": "Scout", "agent_persona_name": "Scout",
                                            "agent_temperature": 0.3})
    assert voice["persona_name"] == "Scout" and voice["temperature"] == 0.3 and voice["max_tokens"] is None


def test_agent_chat_under_a_loadout_drops_the_shared_persona(monkeypatch):
    shared = chat_helpers.PresetInfo(temperature=1.2, max_tokens=50, character_name="Socrates",
                                     system_prompt="Your name is Socrates. Answer with questions.")
    settings = {"s-loadout": {"agent_profile": "Scout", "agent_persona_name": "Scout", "agent_temperature": 0.3},
                "s-plain": {}}
    monkeypatch.setattr(database, "get_session_settings", lambda sid, **kw: settings.get(sid, {}))

    voiced = chat_helpers.loadout_preset("s-loadout", shared)
    assert voiced.system_prompt is None
    assert voiced.character_name == "Scout"
    assert voiced.temperature == 0.3
    from src.constants import DEFAULT_MAX_TOKENS
    assert voiced.max_tokens == DEFAULT_MAX_TOKENS  # not the shared persona's 50

    assert chat_helpers.loadout_preset("s-plain", shared) is shared


def test_customization_block_names_the_loadout_persona():
    block = agent_loop._scoped_agent_customization("Terse field notes.", persona_name="Scout")
    assert "Your name is Scout.\nTerse field notes." in block
    compact = agent_loop._scoped_agent_customization("", compact=True, persona_name="Scout")
    assert "Your name is Scout." in compact and "You are Odysseus" not in compact
    assert agent_loop._scoped_agent_customization("", persona_name="") == ""


def test_workers_get_the_loadout_voice_even_when_called_with_defaults(monkeypatch):
    """Headless workers call the loop with its default sampling; the chat's
    loadout still decides the persona, temperature and reply cap."""
    sent = {}
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "_classify_agent_request", lambda messages, latest: {
        "low_signal": True, "continuation": False, "domains": [], "retrieval_query": latest})
    monkeypatch.setattr(agent_loop, "_is_casual_low_signal", lambda latest: True)
    monkeypatch.setattr(database, "get_session_settings", lambda sid, **kw: {
        "agent_profile": "Scout", "agent_persona_name": "Scout",
        "agent_instructions": "Terse field notes.", "agent_temperature": 0.25, "agent_max_tokens": 64})

    async def fake_stream(candidates, messages, **kwargs):
        sent["messages"] = kwargs["candidate_request_factory"](0, *candidates[0])["messages"]
        sent["temperature"] = kwargs["temperature"]
        sent["max_tokens"] = kwargs["max_tokens"]
        yield 'data: {"delta": "Noted."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    _collect(agent_loop.stream_agent_loop(
        "https://x.example/v1", "generic-model", [{"role": "user", "content": "hi"}],
        relevant_tools=set(), session_id="w1", _is_teacher_run=True,
    ))
    assert sent["temperature"] == 0.25
    assert sent["max_tokens"] == 64
    system = "\n".join(m["content"] for m in sent["messages"] if m["role"] == "system")
    assert "Your name is Scout." in system and "Terse field notes." in system


def test_browser_keeps_the_shared_prompt_out_of_loadout_agents():
    chat = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    menu = (ROOT / "static/js/agentMenu.js").read_text(encoding="utf-8")
    settings_js = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
    dashboard = (ROOT / "static/js/agentsDashboard.js").read_text(encoding="utf-8")
    style = (ROOT / "static/style.css").read_text(encoding="utf-8")
    # Inject text and the persona label come from the loadout in that case.
    assert "(!_sharedPersonaSuppressed() && presetsModule.getInject)" in chat
    assert chat.count("_personaNameForTurn()") >= 3
    assert "export function sharedPersonaSuppressed()" in menu
    assert "body.composer-agent-mode.loadout-voice #character-indicator-btn" in style
    # Both editors can set each agent's voice.
    for key in ("persona_name", "'temperature'", "'max_tokens'", "Start from persona"):
        assert key in settings_js
    for key in ('data-config="agent_persona_name"', 'data-config="agent_temperature"', "agent_max_tokens: draft.agent_max_tokens"):
        assert key in dashboard
    assert "odysseus:loadout-changed" in dashboard and "odysseus:loadout-changed" in menu

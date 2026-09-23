"""The crew member ↔ agent profile link and its precedence rule.

See src/crew_profile.py for the rule these tests pin down:
  1. tool allowlists intersect (deny sets union) — a role can only narrow
  2. every other runtime policy comes from the profile
  3. personality and instructions compose, personality first
  4. no linked profile changes nothing
"""

import json
from types import SimpleNamespace

import pytest

from src import crew_profile
from src.session_settings import stored_disabled_tools


def crew(**kw):
    base = dict(id="crew-1", name="Marketing Mary", owner="alice", personality=None,
                enabled_tools=None, agent_profile=None, session_id="sess-1")
    base.update(kw)
    return SimpleNamespace(**base)


def profile(**kw):
    """A validated profile, so these tests exercise the real shape."""
    from src.agent_profiles import validate_profiles

    base = {"name": "Marketing"}
    base.update(kw)
    return validate_profiles([base])[0]


@pytest.fixture
def profiles(monkeypatch):
    """An in-memory `agent_profiles` store."""
    store = {"rows": []}

    def _load():
        return list(store["rows"])

    monkeypatch.setattr("src.agent_profiles.load_profiles", _load)
    return store


def builtin(name: str) -> bool:
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS

    return name in BUILTIN_TOOL_DESCRIPTIONS


# ── 4. no link, no change ───────────────────────────────────────────────── #

def test_unlinked_crew_has_no_opinion(profiles):
    member = crew(personality="You are Mary.", enabled_tools=json.dumps(["web_search"]))
    assert crew_profile.profile_for_crew(member) is None
    assert crew_profile.session_policy_patch(member) == {}
    # Its own allowlist still inverts exactly as the scheduler always did.
    assert crew_profile.effective_disabled_tools(member) == crew_profile.crew_disabled_tools(member)


def test_no_crew_at_all_is_inert(profiles):
    assert crew_profile.session_policy_patch(None) == {}
    assert crew_profile.effective_disabled_tools(None) == set()
    assert crew_profile.role_system_prompt(None) == ""
    assert crew_profile.merge_crew_disabled_tools(None, ["bash"]) == ["bash"]


def test_link_to_a_deleted_profile_falls_back_to_the_crews_own_fields(profiles):
    member = crew(agent_profile="Gone", personality="You are Mary.",
                  enabled_tools=json.dumps(["web_search"]))
    assert crew_profile.profile_for_crew(member) is None
    assert crew_profile.session_policy_patch(member) == {}
    assert "bash" in crew_profile.effective_disabled_tools(member)


# ── 1. tool allowlists intersect ────────────────────────────────────────── #

def test_crew_allowlist_inverts_to_a_deny_set():
    member = crew(enabled_tools=json.dumps(["web_search", "manage_notes"]))
    denied = crew_profile.crew_disabled_tools(member)
    assert "web_search" not in denied and "manage_notes" not in denied
    assert "bash" in denied


@pytest.mark.parametrize("raw", [None, "", "all", '"all"', "[]", "not json", "{}"])
def test_an_unscoped_crew_denies_nothing(raw):
    assert crew_profile.crew_disabled_tools(crew(enabled_tools=raw)) == set()


def test_a_profile_cannot_widen_the_crews_own_allowlist(profiles):
    # The crew may use web_search and manage_notes. The profile allows a much
    # larger set that includes bash. Linking it must NOT hand the crew bash.
    profiles["rows"] = [profile(name="Marketing", tool_access="selected",
                                enabled_tools=["web_search", "bash", "read_file"])]
    member = crew(agent_profile="Marketing",
                  enabled_tools=json.dumps(["web_search", "manage_notes"]))
    denied = stored_disabled_tools(crew_profile.session_policy_patch(member))
    assert "bash" in denied, "the profile widened a scoped crew member"
    assert "web_search" not in denied, "the intersection dropped a tool both sides allow"
    # manage_notes is allowed by the crew but not by the profile → denied.
    assert "manage_notes" in denied


def test_the_crew_cannot_widen_the_profiles_denylist(profiles):
    profiles["rows"] = [profile(name="Marketing", disabled_tools=["bash", "python"])]
    member = crew(agent_profile="Marketing", enabled_tools=json.dumps(["bash", "web_search"]))
    denied = stored_disabled_tools(crew_profile.session_policy_patch(member))
    assert {"bash", "python"} <= denied


def test_merge_is_a_union_of_denies():
    member = crew(enabled_tools=json.dumps(["web_search"]))
    merged = crew_profile.merge_crew_disabled_tools(member, ["some_mcp_tool"])
    assert "some_mcp_tool" in merged
    assert "bash" in merged
    assert merged == sorted(set(merged)), "the merged deny list must be sorted and unique"


def test_an_unscoped_crew_with_a_permissive_profile_denies_nothing(profiles):
    profiles["rows"] = [profile(name="Marketing")]          # tool_access defaults to "all"
    member = crew(agent_profile="Marketing")
    patch = crew_profile.session_policy_patch(member)
    # Whatever session_patch itself produced, untouched — an unscoped crew
    # member adds no second opinion about how a loadout is stored.
    from src.agent_profiles import session_patch

    assert patch == session_patch(profiles["rows"][0])
    assert not stored_disabled_tools(patch)


# ── 2. the rest of the loadout comes from the profile ───────────────────── #

def test_profile_supplies_the_runtime_policy_session_patch_writes(profiles):
    profiles["rows"] = [profile(name="Marketing", memory_access="none", skill_access="selected",
                                skill_names=["brand-voice"], model_access="selected",
                                allowed_models=["gpt-4o"], mcp_access="none",
                                private_vault_access=True, approval_mode="ask_all",
                                delegation_policy="never", max_parallel_workers=0)]
    member = crew(agent_profile="Marketing")
    patch = crew_profile.session_policy_patch(member)
    assert patch["agent_profile"] == "Marketing"
    assert patch["memory_access"] == "none"
    assert patch["skill_access"] == "selected" and patch["skill_names"] == ["brand-voice"]
    assert patch["model_access"] == "selected" and patch["allowed_models"] == ["gpt-4o"]
    assert patch["allowed_mcp_servers"] == []
    assert patch["private_vault_access"] is True
    assert patch["approval_mode"] == "ask_all"
    assert patch["delegation_policy"] == "never"
    assert patch["max_parallel_workers"] == 0


def test_inherit_approval_mode_is_left_alone(profiles):
    profiles["rows"] = [profile(name="Marketing", approval_mode="inherit")]
    assert "approval_mode" not in crew_profile.session_policy_patch(crew(agent_profile="Marketing"))


# ── 3. prompt text composes ─────────────────────────────────────────────── #

def test_personality_then_instructions(profiles):
    profiles["rows"] = [profile(name="Marketing", instructions="Always cite the brand guide.")]
    member = crew(agent_profile="Marketing", personality="You are Marketing Mary.")
    assert crew_profile.role_system_prompt(member) == (
        "You are Marketing Mary.\n\nAlways cite the brand guide."
    )


def test_either_half_alone(profiles):
    profiles["rows"] = [profile(name="Marketing", instructions="Cite the brand guide.")]
    assert crew_profile.role_system_prompt(crew(agent_profile="Marketing")) == "Cite the brand guide."
    profiles["rows"] = [profile(name="Marketing")]
    assert crew_profile.role_system_prompt(
        crew(agent_profile="Marketing", personality="You are Mary.")) == "You are Mary."
    assert crew_profile.role_system_prompt(crew(agent_profile="Marketing")) == ""


def test_the_chat_role_prompt_needs_the_link(monkeypatch, profiles):
    """A personality alone must not start appearing in chat — that would change
    every existing crew chat. Assigning a role is the opt-in."""
    profiles["rows"] = [profile(name="Marketing", instructions="Cite the brand guide.")]
    member = crew(personality="You are Mary.")
    monkeypatch.setattr(crew_profile, "crew_for_session", lambda sid: member)
    assert crew_profile.role_system_prompt_for_session("sess-1") == ""
    member.agent_profile = "Marketing"
    assert crew_profile.role_system_prompt_for_session("sess-1") == (
        "You are Mary.\n\nCite the brand guide."
    )


# ── applying the link to a chat ─────────────────────────────────────────── #

def test_apply_persists_the_patch_so_later_readers_see_it(monkeypatch, profiles):
    profiles["rows"] = [profile(name="Marketing", tool_access="selected",
                                enabled_tools=["web_search"], memory_access="none")]
    member = crew(agent_profile="Marketing")
    stored = {"sess-1": {"parent_session": "p"}}
    monkeypatch.setattr(crew_profile, "crew_for_session", lambda sid: member)
    monkeypatch.setattr("core.database.get_session_settings", lambda sid: dict(stored.get(sid, {})))

    def _update(sid, patch):
        current = stored.setdefault(sid, {})
        for key, value in patch.items():
            current.pop(key, None) if value is None else current.__setitem__(key, value)
        return dict(current)

    monkeypatch.setattr("core.database.update_session_settings", _update)

    got_crew, settings = crew_profile.apply_crew_profile_to_session("sess-1")
    assert got_crew is member
    assert settings["agent_profile"] == "Marketing"
    assert settings["memory_access"] == "none"
    assert "bash" in stored_disabled_tools(settings)
    assert settings["parent_session"] == "p", "unrelated session settings were clobbered"
    # Persisted, not just returned: agent_loop / tool_execution / private_access
    # re-read these mid-turn, which is what makes the role unbypassable.
    assert stored["sess-1"]["memory_access"] == "none"
    # Idempotent — running it again is the same write.
    assert crew_profile.apply_crew_profile_to_session("sess-1")[1] == settings


def test_apply_is_a_plain_read_when_there_is_no_link(monkeypatch, profiles):
    monkeypatch.setattr(crew_profile, "crew_for_session", lambda sid: crew())
    monkeypatch.setattr("core.database.get_session_settings", lambda sid: {"disabled_tools": ["bash"]})
    monkeypatch.setattr("core.database.update_session_settings",
                        lambda sid, patch: pytest.fail("wrote settings for an unlinked crew member"))
    _, settings = crew_profile.apply_crew_profile_to_session("sess-1")
    assert settings == {"disabled_tools": ["bash"]}


def test_a_failed_write_still_scopes_this_turn(monkeypatch, profiles):
    """A locked or missing row must not mean an unscoped agent."""
    profiles["rows"] = [profile(name="Marketing", tool_access="selected", enabled_tools=["web_search"])]
    monkeypatch.setattr(crew_profile, "crew_for_session", lambda sid: crew(agent_profile="Marketing"))
    monkeypatch.setattr("core.database.get_session_settings", lambda sid: {})
    monkeypatch.setattr("core.database.update_session_settings", lambda sid, patch: None)
    _, settings = crew_profile.apply_crew_profile_to_session("sess-1")
    assert "bash" in stored_disabled_tools(settings)
    assert settings["agent_profile"] == "Marketing"

"""An agent may author and start worker loadouts, but never a bigger one than it has.

`src.agent_loadouts.clamp` is the rule: every capability in a loadout an agent
creates is intersected with the calling chat's own policy before it is stored.
These tests drive that function and the `manage_agent_loadout` tool directly.
"""

import pytest

from src import agent_loadouts
from src.agent_tools.loadout_tools import manage_agent_loadout


UNRESTRICTED = {
    "memory_access": "write", "skill_access": "all", "model_access": "all",
    "allowed_mcp_servers": ["*"], "private_vault_access": True,
    "delegation_policy": "auto", "max_parallel_workers": 8, "approval_mode": "auto",
}


def policy(**overrides):
    """A caller policy with the given restrictions; everything else is open."""
    known = {"bash", "read_file", "write_file", "web_search", "grep", "send_to_session"}
    base = {
        "allowed_tools": set(known), "known_tools": set(known),
        "skill_names": set(), "allowed_models": set(),
        **UNRESTRICTED,
    }
    base.update(overrides)
    return base


def request(**fields):
    return {"name": "Helper", **fields}


# ── tools ────────────────────────────────────────────────────────────────────

def test_a_loadout_cannot_enable_a_tool_the_calling_chat_is_denied():
    caller = policy(allowed_tools={"read_file", "grep"})
    profile, notes = agent_loadouts.clamp(
        request(tool_access="selected", enabled_tools=["read_file", "bash", "write_file"]), caller)
    assert profile["enabled_tools"] == ["read_file"]
    assert "bash" in profile["disabled_tools"] and "write_file" in profile["disabled_tools"]
    assert any("bash" in note for note in notes)


def test_tool_access_all_means_all_the_caller_has_not_every_tool_that_exists():
    caller = policy(allowed_tools={"read_file", "grep"})
    profile, _ = agent_loadouts.clamp(request(tool_access="all"), caller)
    assert profile["tool_access"] == "selected"
    assert profile["enabled_tools"] == ["grep", "read_file"]
    assert "bash" in profile["disabled_tools"]


def test_a_loadout_with_nothing_left_is_stored_as_no_tools():
    caller = policy(allowed_tools=set())
    profile, _ = agent_loadouts.clamp(request(tool_access="all"), caller)
    assert profile["tool_access"] == "none"
    assert profile["enabled_tools"] == []


def test_large_inventory_uses_authoritative_positive_policy_not_truncated_denylist():
    """None/selected stays binding without copying hundreds of denied names."""
    many = {f"tool_{i}" for i in range(agent_loadouts.MAX_DISABLED_TOOLS_IN_PROFILE + 5)}
    caller = policy(allowed_tools=set(), known_tools=many)
    profile, _ = agent_loadouts.clamp(request(tool_access="none"), caller)
    assert profile["tool_access"] == "none"
    assert profile["enabled_tools"] == []
    assert profile["disabled_tools"] == []


# ── graded policies ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("field, caller_value, asked, expected", [
    ("memory_access", "read", "write", "read"),
    ("memory_access", "none", "read", "none"),
    ("memory_access", "write", "read", "read"),          # narrower is fine
    ("skill_access", "none", "all", "none"),
    ("delegation_policy", "never", "auto", "never"),
    ("delegation_policy", "explicit", "auto", "explicit"),
    ("delegation_policy", "auto", "never", "never"),      # narrower is fine
])
def test_graded_policies_are_capped_at_the_callers_level(field, caller_value, asked, expected):
    profile, _ = agent_loadouts.clamp(request(**{field: asked}), policy(**{field: caller_value}))
    assert profile[field] == expected


def test_approval_mode_may_be_stricter_but_never_looser():
    strict = policy(approval_mode="ask_all")
    assert agent_loadouts.clamp(request(approval_mode="auto"), strict)[0]["approval_mode"] == "ask_all"
    assert agent_loadouts.clamp(request(approval_mode="ask_all"), policy())[0]["approval_mode"] == "ask_all"


def test_parallel_worker_budget_is_capped():
    profile, notes = agent_loadouts.clamp(request(max_parallel_workers=8), policy(max_parallel_workers=2))
    assert profile["max_parallel_workers"] == 2
    assert any("max_parallel_workers" in note for note in notes)


def test_private_vault_access_is_not_self_granted():
    profile, notes = agent_loadouts.clamp(request(private_vault_access=True),
                                          policy(private_vault_access=False))
    assert profile["private_vault_access"] is False
    assert any("private_vault_access" in note for note in notes)


def test_private_vault_access_passes_through_when_the_caller_holds_it():
    profile, _ = agent_loadouts.clamp(request(private_vault_access=True),
                                      policy(private_vault_access=True))
    assert profile["private_vault_access"] is True


# ── models and connections ───────────────────────────────────────────────────

def test_a_chat_pinned_to_its_model_cannot_author_a_model_switcher():
    profile, notes = agent_loadouts.clamp(
        request(model="gpt-oss", model_access="all", allowed_models=["a", "b"]),
        policy(model_access="current"))
    assert profile["model_access"] == "current"
    assert profile["model"] == "" and profile["allowed_models"] == []
    assert notes


def test_allowed_models_are_intersected_with_the_callers_list():
    caller = policy(model_access="selected", allowed_models={"qwen3", "llama"})
    profile, _ = agent_loadouts.clamp(
        request(model_access="selected", allowed_models=["qwen3", "secret-model"], model="secret-model"),
        caller)
    assert profile["allowed_models"] == ["qwen3"]
    assert profile["model"] == ""


def test_mcp_all_collapses_to_the_callers_own_connections():
    caller = policy(allowed_mcp_servers=["email"])
    profile, _ = agent_loadouts.clamp(request(mcp_access="all"), caller)
    assert profile["mcp_access"] == "selected"
    assert profile["allowed_mcp_servers"] == ["email"]


def test_mcp_selection_outside_the_callers_list_is_dropped():
    caller = policy(allowed_mcp_servers=["email"])
    profile, _ = agent_loadouts.clamp(
        request(mcp_access="selected", allowed_mcp_servers=["email", "github"]), caller)
    assert profile["allowed_mcp_servers"] == ["email"]


def test_an_unrestricted_caller_keeps_what_it_asked_for():
    profile, notes = agent_loadouts.clamp(
        request(tool_access="selected", enabled_tools=["bash"], memory_access="write",
                delegation_policy="auto", max_parallel_workers=4, private_vault_access=True),
        policy())
    assert profile["enabled_tools"] == ["bash"]
    assert profile["memory_access"] == "write"
    assert profile["max_parallel_workers"] == 4
    assert profile["private_vault_access"] is True
    assert [n for n in notes if "tools" in n] == []


# ── the tool surface ─────────────────────────────────────────────────────────

@pytest.fixture
def store(monkeypatch):
    """An in-memory agent_profiles store and an unrestricted calling chat."""
    saved = {"profiles": []}
    monkeypatch.setattr(agent_loadouts, "_write",
                        lambda profiles: saved.__setitem__("profiles", list(profiles)))
    monkeypatch.setattr("src.agent_profiles.load_profiles", lambda: list(saved["profiles"]))
    monkeypatch.setattr(agent_loadouts, "caller_policy", lambda sid, owner: policy())
    return saved


async def test_create_then_list_then_delete(store):
    created = await manage_agent_loadout(
        '{"action": "create", "name": "Reviewer", "description": "reads diffs",'
        ' "tool_access": "selected", "enabled_tools": ["read_file", "grep"]}', "chat-1", owner="u")
    assert created["exit_code"] == 0
    assert created["loadout"]["tools"] == ["grep", "read_file"]

    listed = await manage_agent_loadout('{"action": "list"}', "chat-1", owner="u")
    assert [row["name"] for row in listed["loadouts"]] == ["Reviewer"]
    assert listed["loadouts"][0]["tool_count"] == 2
    assert "tools" not in listed["loadouts"][0]

    removed = await manage_agent_loadout('{"action": "delete", "name": "Reviewer"}', "chat-1", owner="u")
    assert removed["exit_code"] == 0
    assert store["profiles"] == []


async def test_create_refuses_to_overwrite_and_update_replaces(store):
    body = '{"action": "%s", "name": "Reviewer", "description": "%s"}'
    assert (await manage_agent_loadout(body % ("create", "first"), "c", owner="u"))["exit_code"] == 0
    clash = await manage_agent_loadout(body % ("create", "second"), "c", owner="u")
    assert clash["exit_code"] == 1 and "already exists" in clash["error"]
    updated = await manage_agent_loadout(body % ("update", "second"), "c", owner="u")
    assert updated["exit_code"] == 0
    assert store["profiles"][0]["description"] == "second"


async def test_capabilities_reports_the_ceiling_before_the_agent_trips_over_it(store):
    result = await manage_agent_loadout('{"action": "capabilities"}', "c", owner="u")
    assert result["exit_code"] == 0
    assert "bash" in result["ceiling"]["tool_examples"]
    assert result["ceiling"]["tool_count"] >= len(result["ceiling"]["tool_examples"])

    detailed = await manage_agent_loadout('{"action": "capabilities", "detail": true}', "c", owner="u")
    assert "bash" in detailed["ceiling"]["tools"]
    assert result["ceiling"]["max_parallel_workers"] == 8


async def test_narrowing_is_reported_not_silent(monkeypatch, store):
    monkeypatch.setattr(agent_loadouts, "caller_policy",
                        lambda sid, owner: policy(allowed_tools={"grep"}, private_vault_access=False))
    result = await manage_agent_loadout(
        '{"action": "create", "name": "Snoop", "tool_access": "selected",'
        ' "enabled_tools": ["bash", "grep"], "private_vault_access": true}', "c", owner="u")
    assert result["exit_code"] == 0
    assert result["loadout"]["tools"] == ["grep"]
    assert result["loadout"]["private_vault_access"] is False
    assert len(result["narrowed"]) >= 2
    assert "narrowed" in result["response"]


async def test_start_needs_a_task(store):
    result = await manage_agent_loadout('{"action": "start"}', "c", owner="u")
    assert result["exit_code"] == 1 and "task" in result["error"]


async def test_start_is_refused_when_the_chat_may_not_delegate(monkeypatch, store):
    monkeypatch.setattr(agent_loadouts, "caller_policy",
                        lambda sid, owner: policy(delegation_policy="never"))
    result = await manage_agent_loadout('{"action": "start", "task": "go"}', "c", owner="u")
    assert result["exit_code"] == 1 and "delegation policy" in result["error"]


async def test_start_respects_the_same_worker_limit_as_the_spawning_tools(monkeypatch, store):
    monkeypatch.setattr(agent_loadouts, "caller_policy",
                        lambda sid, owner: policy(max_parallel_workers=1))
    monkeypatch.setattr("src.agent_control.live_children", lambda sid: 1)
    result = await manage_agent_loadout('{"action": "start", "task": "go"}', "c", owner="u")
    assert result["exit_code"] == 1 and result["blocked_reason"] == "worker_capacity"
    assert result["capacity"] == {"limit": 1, "active": 1, "available": 0}


async def test_start_launches_a_worker_reporting_to_the_calling_chat(monkeypatch, store):
    await manage_agent_loadout('{"action": "create", "name": "Runner"}', "c", owner="u")
    seen = {}

    async def fake_launch(**kwargs):
        seen.update(kwargs)
        return {"session_id": "w-1", "session_name": "Runner 1", "run_id": "r-1"}

    monkeypatch.setattr("src.agent_control.launch_worker", fake_launch)
    monkeypatch.setattr("src.agent_control.live_children", lambda sid: 0)
    result = await manage_agent_loadout(
        '{"action": "start", "name": "Runner", "task": "summarise the changelog"}', "chat-7", owner="u")
    assert result["exit_code"] == 0
    assert seen["profile_name"] == "Runner"
    assert seen["parent_session"] == "chat-7"
    assert seen["task"] == "summarise the changelog"
    assert result["session_id"] == "w-1"


async def test_start_rejects_an_unknown_loadout(store):
    result = await manage_agent_loadout('{"action": "start", "name": "Nope", "task": "go"}', "c", owner="u")
    assert result["exit_code"] == 1 and "no loadout named" in result["error"]


async def test_an_unknown_action_is_refused(store):
    result = await manage_agent_loadout('{"action": "escalate"}', "c", owner="u")
    assert result["exit_code"] == 1 and "action must be one of" in result["error"]


# ── the ceiling comes from the real per-chat policy ──────────────────────────

def test_caller_policy_reads_the_chats_own_stored_settings(monkeypatch):
    """The clamp is only meaningful if its ceiling is the policy the Control
    Room writes and agent_loop enforces, not a default."""
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **kwargs: {
        "disabled_tools": ["bash", "write_file"],
        "memory_access": "read",
        "model_access": "current",
        "allowed_mcp_servers": ["email"],
        "private_vault_access": True,
        "delegation_policy": "never",
        "max_parallel_workers": 0,
    })
    monkeypatch.setattr("src.tool_security.owner_baseline_disabled_tools", lambda owner: set())
    result = agent_loadouts.caller_policy("chat-1", "owner")
    assert "bash" not in result["allowed_tools"]
    assert "write_file" not in result["allowed_tools"]
    assert "read_file" in result["allowed_tools"]
    assert result["memory_access"] == "read"
    assert result["model_access"] == "current"
    assert result["allowed_mcp_servers"] == ["email"]
    assert result["private_vault_access"] is True
    assert result["delegation_policy"] == "never"
    assert result["max_parallel_workers"] == 0


def test_caller_policy_also_applies_the_owner_baseline(monkeypatch):
    """A tool the operator switched off globally is not available to a loadout
    just because this chat never denied it individually."""
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **kwargs: {})
    monkeypatch.setattr("src.tool_security.owner_baseline_disabled_tools", lambda owner: {"web_search"})
    result = agent_loadouts.caller_policy("chat-1", "owner")
    assert "web_search" not in result["allowed_tools"]


def test_an_unconfigured_chat_is_not_treated_as_a_locked_down_one(monkeypatch):
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **kwargs: {})
    monkeypatch.setattr("src.tool_security.owner_baseline_disabled_tools", lambda owner: set())
    result = agent_loadouts.caller_policy("chat-1", "owner")
    assert result["memory_access"] == "write"
    assert result["model_access"] == "all"
    assert result["allowed_mcp_servers"] == ["*"]
    assert result["private_vault_access"] is False


# ── owner scope on the start action ──────────────────────────────────────────

class _Chat:
    def __init__(self, owner):
        self.owner = owner


def _manager(sessions):
    class M:
        def get_session(self, sid):
            return sessions.get(sid)
    return M()


@pytest.fixture
def launcher(monkeypatch, store):
    seen = {}

    async def fake_launch(**kwargs):
        seen.update(kwargs)
        return {"session_id": "w-1", "session_name": "W", "run_id": "r-1"}

    monkeypatch.setattr("src.agent_control.launch_worker", fake_launch)
    monkeypatch.setattr("src.agent_control.live_children", lambda sid: 0)
    return seen


async def test_start_refuses_another_owners_chat_as_the_parent(monkeypatch, launcher):
    monkeypatch.setattr("src.ai_interaction.get_session_manager",
                        lambda: _manager({"theirs": _Chat("someone-else")}))
    result = await manage_agent_loadout(
        '{"action": "start", "task": "go", "parent_session": "theirs"}', "mine", owner="me")
    assert result["exit_code"] == 1 and "not found" in result["error"]
    assert launcher == {}


async def test_start_accepts_another_chat_the_caller_does_own(monkeypatch, launcher):
    monkeypatch.setattr("src.ai_interaction.get_session_manager",
                        lambda: _manager({"other": _Chat("me")}))
    result = await manage_agent_loadout(
        '{"action": "start", "task": "go", "parent_session": "other"}', "mine", owner="me")
    assert result["exit_code"] == 0
    assert launcher["parent_session"] == "other"


async def test_start_can_be_asked_for_a_standalone_worker(launcher):
    result = await manage_agent_loadout(
        '{"action": "start", "task": "go", "parent_session": ""}', "mine", owner="me")
    assert result["exit_code"] == 0
    assert launcher["parent_session"] is None


# ── update must not wipe fields the caller never set ─────────────────────────
#
# Native function-calling providers fill every declared property. A real call
# recorded in the logs came through as
#   {"action": "capabilities", "name": "", "task": "", "instructions": "", ...}
# — every property present, most of them blank. An `update` that rebuilt the
# profile from that payload reset each untouched field to validate_profiles'
# default, which for `tool_access` is "all": changing a description silently
# re-granted a deliberately narrow loadout every tool its author could use.

async def test_update_only_changes_the_fields_the_caller_supplied(store):
    await manage_agent_loadout(
        '{"action": "create", "name": "Reviewer", "description": "reads diffs",'
        ' "instructions": "Be terse.", "tool_access": "selected",'
        ' "enabled_tools": ["read_file", "grep"], "memory_access": "none",'
        ' "max_rounds": 5}', "c", owner="u")

    # Exactly the shape a provider sends: one real edit, every other property blank.
    await manage_agent_loadout(
        '{"action": "update", "name": "Reviewer", "description": "reviews diffs",'
        ' "instructions": "", "model": "", "tool_access": "", "enabled_tools": [],'
        ' "memory_access": "", "skill_names": [], "max_rounds": 0}', "c", owner="u")

    saved = store["profiles"][0]
    assert saved["description"] == "reviews diffs"
    assert saved["instructions"] == "Be terse."
    assert saved["tool_access"] == "selected"
    assert saved["enabled_tools"] == ["grep", "read_file"]
    assert saved["memory_access"] == "none"
    assert saved["max_rounds"] == 5


async def test_update_does_not_re_widen_a_narrow_tool_policy(store):
    """The sharp edge of the same bug: a blank tool_access defaulting to 'all'."""
    await manage_agent_loadout(
        '{"action": "create", "name": "Narrow", "tool_access": "selected",'
        ' "enabled_tools": ["grep"]}', "c", owner="u")
    await manage_agent_loadout(
        '{"action": "update", "name": "Narrow", "description": "now documented"}',
        "c", owner="u")
    assert store["profiles"][0]["enabled_tools"] == ["grep"]
    assert store["profiles"][0]["tool_access"] == "selected"


async def test_a_field_can_still_be_cleared_on_purpose(store):
    """Ignoring blanks must not make a field impossible to unset."""
    await manage_agent_loadout(
        '{"action": "create", "name": "Reviewer", "instructions": "Be terse."}',
        "c", owner="u")
    result = await manage_agent_loadout(
        '{"action": "update", "name": "Reviewer", "clear": ["instructions"]}',
        "c", owner="u")
    assert result["exit_code"] == 0
    assert store["profiles"][0]["instructions"] == ""


async def test_update_of_an_unknown_loadout_is_refused(store):
    result = await manage_agent_loadout(
        '{"action": "update", "name": "Ghost", "description": "x"}', "c", owner="u")
    assert result["exit_code"] == 1 and "no loadout named" in result["error"]


async def test_zero_parallel_workers_is_a_real_setting_not_a_blank(store):
    """0 means 'this worker starts no children'. It must survive an update the
    way an empty string must not."""
    await manage_agent_loadout(
        '{"action": "create", "name": "Solo", "max_parallel_workers": 0}', "c", owner="u")
    assert store["profiles"][0]["max_parallel_workers"] == 0
    await manage_agent_loadout(
        '{"action": "update", "name": "Solo", "description": "no children",'
        ' "max_parallel_workers": 0}', "c", owner="u")
    assert store["profiles"][0]["max_parallel_workers"] == 0


async def test_update_does_not_rename_by_recapitalising(store):
    await manage_agent_loadout('{"action": "create", "name": "Reviewer"}', "c", owner="u")
    await manage_agent_loadout(
        '{"action": "update", "name": "reviewer", "description": "x"}', "c", owner="u")
    assert [p["name"] for p in store["profiles"]] == ["Reviewer"]

"""Picking the agent for the user (src/loadout_routing.py)."""

from src.agent_profiles import validate_profiles
from src.loadout_routing import loadout_named_in, routing_note, suggest_loadouts

PROFILES = validate_profiles([
    {"name": "Penpot Product Designer", "persona_name": "Penpot Product Designer",
     "description": "Designs product UI in Penpot", "tool_access": "selected",
     "enabled_tools": ["read_file", "mcp__c5ec6d7a__*"]},
    {"name": "Odysseus Admin", "tool_access": "selected", "enabled_tools": ["read_file", "mcp__*"]},
    {"name": "Researcher", "description": "Web research and source audits",
     "tool_access": "selected", "enabled_tools": ["web_search", "web_fetch"]},
])


def test_a_denied_mcp_tool_points_at_the_loadout_built_for_that_server():
    fits = suggest_loadouts("make a quick mockup", ["mcp__c5ec6d7a__create_project", "update_plan"],
                            current_profile="Odysseus Admin", profiles=PROFILES)
    assert [f["name"] for f in fits] == ["Penpot Product Designer"]
    assert "c5ec6d7a" in fits[0]["reason"]


def test_a_grant_over_every_server_is_not_evidence_of_fit():
    fits = suggest_loadouts("make a quick mockup", ["mcp__c5ec6d7a__create_project"],
                            profiles=PROFILES)
    assert "Odysseus Admin" not in [f["name"] for f in fits]


def test_the_request_naming_the_subject_is_enough():
    fits = suggest_loadouts("use the design agent to create a small penpot test",
                            [], current_profile="Odysseus Admin", profiles=PROFILES)
    assert fits and fits[0]["name"] == "Penpot Product Designer"


def test_unrelated_requests_and_the_chats_own_loadout_suggest_nothing():
    assert suggest_loadouts("what is on my calendar tomorrow", [], profiles=PROFILES) == []
    assert suggest_loadouts("penpot mockup", ["mcp__c5ec6d7a__create_project"],
                            current_profile="penpot product designer", profiles=PROFILES) == []


def test_naming_a_saved_loadout_is_recognised():
    assert loadout_named_in("yes, start Penpot Product Designer", PROFILES) == {"Penpot Product Designer"}
    assert loadout_named_in("research this for me", PROFILES) == set()


def test_the_note_says_whether_the_model_may_launch():
    fits = [{"name": "Penpot Product Designer", "reason": "x", "tools": []}]
    assert "manage_agent_loadout" in routing_note(fits, may_launch=True)
    assert "ask whether to start it" in routing_note(fits, may_launch=False)
    assert routing_note([], may_launch=True) == ""

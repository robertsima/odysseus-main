"""An agent's quick actions: validated when saved, kept through the round trips.

The production bug: a loadout saved from a chat ("Save to loadout") rebuilt the
loadout from the chat's settings and dropped everything the chat does not store,
so an agent lost its quick actions the first time someone saved it that way.
"""
import pytest

from src import agent_profiles


def _one(**raw):
    return agent_profiles.validate_profiles([{"name": "A", **raw}])[0]


def test_valid_quick_actions_are_kept_trimmed():
    out = _one(quick_actions=[{"label": " Revise ", "prompt": " Revise {document} "}])
    assert out["quick_actions"] == [{"label": "Revise", "prompt": "Revise {document}"}]


def test_no_quick_actions_is_an_empty_list():
    assert _one()["quick_actions"] == []


def test_blank_editor_rows_are_dropped():
    out = _one(quick_actions=[{"label": "", "prompt": ""}, {"label": "Ok", "prompt": "Do it"}])
    assert [q["label"] for q in out["quick_actions"]] == ["Ok"]


@pytest.mark.parametrize("bad", [
    [{"label": "x" * 41, "prompt": "p"}],
    [{"label": "x", "prompt": "p" * 2001}],
    [{"label": "only a label", "prompt": ""}],
    [{"label": "", "prompt": "only a prompt"}],
    [{"label": str(i), "prompt": "p"} for i in range(7)],
    "not a list",
    ["not an object"],
])
def test_bad_quick_actions_are_refused(bad):
    with pytest.raises(ValueError):
        _one(quick_actions=bad)


def test_six_quick_actions_and_the_limits_exactly_are_allowed():
    rows = [{"label": "l" * 40, "prompt": "p" * 2000} for _ in range(6)]
    assert len(_one(quick_actions=rows)["quick_actions"]) == 6


def test_saving_a_chat_back_to_its_loadout_keeps_the_quick_actions():
    base = _one(quick_actions=[{"label": "Revise", "prompt": "Revise it"}])
    settings = agent_profiles.session_patch(base)
    back = agent_profiles.validate_profiles([agent_profiles.profile_from_session(base, settings)])[0]
    assert back["quick_actions"] == base["quick_actions"]


def test_exported_and_imported_profiles_carry_them(monkeypatch):
    from src import agent_profile_transfer

    base = _one(quick_actions=[{"label": "Revise", "prompt": "Revise it"}])
    monkeypatch.setattr(agent_profiles, "load_profiles", lambda: [base])
    doc = agent_profile_transfer.export_profiles()
    assert doc["profiles"][0]["quick_actions"] == [{"label": "Revise", "prompt": "Revise it"}]

"""manage_skills: load several skills in one call, and a search that can find
a skill from the words a person would actually use."""
import json

import pytest

from services.memory.skills import SkillsManager
from src.tools import system as system_tools


@pytest.fixture
def library(tmp_path, monkeypatch):
    sm = SkillsManager(str(tmp_path))
    for name, desc, proc in (
        ("claude-code-delegation", "Delegate bounded coding work to Claude Code and verify it", ["run status", "run the task", "inspect the diff"]),
        ("icon-library-selection", "Pick an open-source icon set for a web UI", ["compare licences", "check accessibility"]),
        ("email-triage", "Sort the inbox by urgency", ["list unread", "tag urgent"]),
    ):
        sm.add_skill(name=name, description=desc, category="dev", tags=[], platforms=[],
                     requires_toolsets=[], fallback_for_toolsets=[], when_to_use="",
                     procedure=proc, pitfalls=[], verification=[], status="published",
                     version="1.0.0", confidence=0.9, source="learned", teacher_model=None, owner="u1")
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    return sm


async def _call(payload):
    return await system_tools.do_manage_skills(json.dumps(payload), owner="u1")


async def test_view_loads_several_skills_in_one_call(library):
    res = await _call({"action": "view", "names": ["claude-code-delegation", "icon-library-selection", "nope"]})
    body = res["results"]
    assert "===== skill: claude-code-delegation =====" in body
    assert "===== skill: icon-library-selection =====" in body
    assert "Delegate bounded coding work" in body and "open-source icon set" in body
    assert "Not found: nope" in body


async def test_view_accepts_a_comma_separated_name(library):
    res = await _call({"action": "view", "name": "email-triage, icon-library-selection"})
    assert "===== skill: email-triage =====" in res["results"]
    assert "===== skill: icon-library-selection =====" in res["results"]


async def test_single_view_keeps_the_plain_skill_md_shape(library):
    res = await _call({"action": "view", "name": "email-triage"})
    assert res["results"].lstrip().startswith("---")
    assert "=====" not in res["results"]


async def test_view_of_only_unknown_names_is_an_error(library):
    res = await _call({"action": "view", "names": ["ghost"]})
    assert res["exit_code"] == 1 and "'ghost'" in res["error"]


async def test_search_finds_a_skill_from_natural_words(library):
    res = await _call({"action": "search", "query": "delegate this coding task to claude code"})
    assert "claude-code-delegation" in res["results"]
    assert "email-triage" not in res["results"]

    res = await _call({"action": "search", "query": "which icon library should I use for the UI"})
    assert "icon-library-selection" in res["results"]


def test_relevance_scores_scale_with_the_request(library):
    skills = library.load(owner="u1")
    hits = library.get_relevant_skills("delegate this coding task to claude code", skills=skills)
    assert [s["name"] for s in hits][:1] == ["claude-code-delegation"]

    hits = library.get_relevant_skills("what is the weather in Oslo tomorrow", skills=skills)
    assert hits == [], "an unrelated request must not drag in a skill"

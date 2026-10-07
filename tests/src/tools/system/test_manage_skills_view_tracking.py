"""manage_skills records which skills an agent actually read.

The 2026-10-06 diagnostics showed six `manage_skills` calls with no action or
skill name in the log, and a skill's `uses` counter goes up when the skill is
shown to the model, not when it is read. Nobody could tell which of a
loadout's 18 skills were ever opened.
"""
import json
import logging

import pytest

from services.memory.skills import SkillsManager
from src.tools import system as system_tools


@pytest.fixture
def library(tmp_path, monkeypatch):
    sm = SkillsManager(str(tmp_path))
    for name in ("diagnosing-bugs", "codebase-design"):
        sm.add_skill(name=name, description=f"{name} procedure", category="dev", tags=[],
                     platforms=[], requires_toolsets=[], fallback_for_toolsets=[],
                     when_to_use="", procedure=["step"], pitfalls=[], verification=[],
                     status="published", version="1.0.0", confidence=0.9, source="learned",
                     teacher_model=None, owner="u1")
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    return sm


def _row(sm, name):
    return next(s for s in sm.load(owner="u1") if s["name"] == name)


async def test_viewing_a_skill_counts_a_view_not_a_use(library):
    await system_tools.do_manage_skills(
        json.dumps({"action": "view", "names": ["diagnosing-bugs", "missing-one"]}), owner="u1")

    read = _row(library, "diagnosing-bugs")
    assert read["views"] == 1 and read["last_viewed"]
    assert read["uses"] == 0
    assert _row(library, "codebase-design")["views"] == 0


async def test_each_call_logs_its_action_and_skill_names(library, caplog):
    with caplog.at_level(logging.INFO, logger=system_tools.logger.name):
        await system_tools.do_manage_skills(
            json.dumps({"action": "view", "name": "codebase-design"}), owner="u1")

    line = next(r.getMessage() for r in caplog.records if "[skills]" in r.getMessage())
    assert "action=view" in line and "codebase-design" in line

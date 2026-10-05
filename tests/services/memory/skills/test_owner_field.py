"""A skill update cannot hand the skill to another user.

update_skill copies the scalar fields of ``updates`` onto the skill. If
``owner`` were one of them, any caller allowed to edit a skill could pass
``{"owner": "bob"}`` and give it away, or take one over.
"""
import pytest

from services.memory.skills import SkillsManager

pytestmark = pytest.mark.security


def _named(skills, name):
    return [s for s in skills if s.get("name") == name]


def test_an_update_cannot_change_the_skills_owner(tmp_path):
    manager = SkillsManager(str(tmp_path))
    manager.add_skill(name="login-flow", description="original", category="auth",
                      procedure=["step one"], owner="alice")

    assert manager.update_skill("login-flow", {"owner": "bob", "description": "edited"}, owner="alice")

    [skill] = _named(manager.load(owner="alice"), "login-flow")
    assert skill["description"] == "edited"
    assert _named(manager.load(owner="bob"), "login-flow") == []

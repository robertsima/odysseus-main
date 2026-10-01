from pathlib import Path

from services.memory.skills import SkillsManager
from services.memory.skill_format import Skill
from src import builtin_skills


def _make_bundle(root: Path, body: str = "Bundled instructions.") -> None:
    skill_dir = root / "skills" / "local-pi-delegation"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: local-pi-delegation\n"
        "description: Delegate focused work.\n---\n\n" + body,
        encoding="utf-8",
    )
    (skill_dir / "references" / "profile.md").write_text("profile", encoding="utf-8")


def test_seed_bundled_skill_copies_bundle(monkeypatch, tmp_path):
    app_root = tmp_path / "app"
    _make_bundle(app_root)
    manager = SkillsManager(str(tmp_path / "data"))
    monkeypatch.setattr(builtin_skills, "get_app_root", lambda: str(app_root))

    installed = builtin_skills.seed_bundled_skills(manager)

    destination = Path(manager.skills_root) / "dev" / "local-pi-delegation"
    assert installed == ["local-pi-delegation"]
    installed_skill = Skill.from_markdown(
        (destination / "SKILL.md").read_text(encoding="utf-8")
    )
    assert installed_skill.status == "published"
    assert installed_skill.category == "dev"
    assert installed_skill.source == "bundled"
    assert installed_skill.owner is None
    assert installed_skill.requires_toolsets == ["mcp__pi_worker__run_pi_task"]
    assert "Bundled instructions." in installed_skill.body_extra
    assert [item["name"] for item in manager.index_for(
        active_toolsets=["mcp__pi_worker__run_pi_task"]
    )] == ["local-pi-delegation"]
    assert manager.index_for(active_toolsets=[]) == []
    assert (destination / "references" / "profile.md").is_file()


def test_seed_bundled_skill_preserves_existing_copy(monkeypatch, tmp_path):
    app_root = tmp_path / "app"
    _make_bundle(app_root, "New bundled content.")
    manager = SkillsManager(str(tmp_path / "data"))
    destination = Path(manager.skills_root) / "dev" / "local-pi-delegation"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("operator edit", encoding="utf-8")
    monkeypatch.setattr(builtin_skills, "get_app_root", lambda: str(app_root))

    installed = builtin_skills.seed_bundled_skills(manager)

    assert installed == []
    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "operator edit"


def test_seed_reconciles_legacy_owner_metadata_without_replacing_body(monkeypatch, tmp_path):
    app_root = tmp_path / "app"
    _make_bundle(app_root, "New bundled content.")
    manager = SkillsManager(str(tmp_path / "data"))
    monkeypatch.setattr(builtin_skills, "get_app_root", lambda: str(app_root))
    builtin_skills.seed_bundled_skills(manager)

    skill_path = Path(manager.skills_root) / "dev" / "local-pi-delegation" / "SKILL.md"
    legacy = Skill.from_markdown(skill_path.read_text(encoding="utf-8"))
    legacy.source = "user"
    legacy.owner = "first-account"
    legacy.body_extra = "Operator-customized delegation instructions."
    skill_path.write_text(legacy.to_markdown(), encoding="utf-8")

    reconciled = builtin_skills.seed_bundled_skills(manager)
    result = Skill.from_markdown(skill_path.read_text(encoding="utf-8"))

    assert reconciled == ["local-pi-delegation"]
    assert result.source == "bundled"
    assert result.owner is None
    assert "Operator-customized delegation instructions." in result.body_extra
    assert "New bundled content." not in result.body_extra


_COMMUNITY_SKILLS = {
    "grilling": "general",
    "writing-for-agents": "general",
    "unslop": "general",
    "diagnosing-bugs": "dev",
    "codebase-design": "dev",
    "domain-modeling": "dev",
    "improve-codebase-architecture": "dev",
    "triage": "dev",
    "resolving-merge-conflicts": "dev",
}


def test_every_bundled_skill_has_a_source_directory():
    app_root = Path(__file__).resolve().parents[1]
    for category, name, *_rest in builtin_skills._BUNDLED_SKILLS:
        source = builtin_skills._bundled_source(str(app_root), category, name)
        assert (Path(source) / "SKILL.md").is_file(), f"{category}/{name} has no SKILL.md"


def test_community_skills_parse_and_are_registered():
    app_root = Path(__file__).resolve().parents[1]
    registered = {(c, n) for c, n, *_ in builtin_skills._BUNDLED_SKILLS}
    for name, category in _COMMUNITY_SKILLS.items():
        assert (category, name) in registered
        path = app_root / "skills" / category / name / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        skill = Skill.from_markdown(text, path=str(path))
        assert skill.name == name
        assert skill.category == category
        assert skill.status == "published"
        assert skill.source == "bundled"
        assert skill.description
        # Verified skills must say where their text came from.
        assert "## Provenance" in text and "- License: MIT" in text


def test_community_skills_are_listed_in_acknowledgments():
    app_root = Path(__file__).resolve().parents[1]
    text = (app_root / "ACKNOWLEDGMENTS.md").read_text(encoding="utf-8")
    assert "## Agent skills" in text
    for name in _COMMUNITY_SKILLS:
        assert f"`{name}" in text

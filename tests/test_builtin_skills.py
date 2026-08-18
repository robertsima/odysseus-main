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

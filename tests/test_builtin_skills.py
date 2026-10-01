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


# --- Upgrade of unedited installs (2026-10-01) -------------------------------

import json  # noqa: E402


def _history(app_root: Path, name: str, *bodies: str) -> None:
    digests = [builtin_skills.skill_digest(_skill_text(b)) for b in bodies]
    (app_root / "skills" / ".bundled-history.json").write_text(
        json.dumps({name: digests}), encoding="utf-8"
    )


def _skill_text(body: str) -> str:
    return (
        "---\nname: local-pi-delegation\n"
        "description: Delegate focused work.\n---\n\n" + body
    )


def _setup(monkeypatch, tmp_path, old: str, new: str):
    """Install `old`, then ship `new`; history knows both."""
    app_root = tmp_path / "app"
    _make_bundle(app_root, old)
    manager = SkillsManager(str(tmp_path / "data"))
    monkeypatch.setattr(builtin_skills, "get_app_root", lambda: str(app_root))
    monkeypatch.setattr(builtin_skills, "_customised_logged", set())
    _history(app_root, "local-pi-delegation", old, new)
    builtin_skills.seed_bundled_skills(manager)
    _make_bundle_files(app_root, new)
    dest = Path(manager.skills_root) / "dev" / "local-pi-delegation"
    return app_root, manager, dest


def _make_bundle_files(app_root: Path, body: str) -> None:
    skill_dir = app_root / "skills" / "local-pi-delegation"
    (skill_dir / "SKILL.md").write_text(_skill_text(body), encoding="utf-8")
    (skill_dir / "references" / "profile.md").write_text("profile v2", encoding="utf-8")
    (skill_dir / "references" / "extra.md").write_text("new file", encoding="utf-8")


def _body(dest: Path) -> str:
    return Skill.from_markdown((dest / "SKILL.md").read_text(encoding="utf-8")).body_extra


def test_unedited_old_version_is_upgraded_with_supporting_files(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO")
    _, manager, dest = _setup(monkeypatch, tmp_path, "Old body.", "New body.")
    assert "Old body." in _body(dest)
    (dest / "references" / "stale.md").write_text("gone", encoding="utf-8")

    builtin_skills.seed_bundled_skills(manager)

    assert "New body." in _body(dest)
    assert (dest / "references" / "profile.md").read_text(encoding="utf-8") == "profile v2"
    assert (dest / "references" / "extra.md").is_file()
    assert not (dest / "references" / "stale.md").exists()
    assert "upgraded bundled skill local-pi-delegation" in caplog.text
    # Upgraded installs count as shipped again, so the tool-gate exemption returns.
    entry = next(s for s in manager.load_all() if s["name"] == "local-pi-delegation")
    assert builtin_skills.is_shipped_skill(entry)


def test_edited_install_is_kept_and_logged_once(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO")
    _, manager, dest = _setup(monkeypatch, tmp_path, "Old body.", "New body.")
    skill = Skill.from_markdown((dest / "SKILL.md").read_text(encoding="utf-8"))
    skill.body_extra = "Operator wrote this."
    (dest / "SKILL.md").write_text(skill.to_markdown(), encoding="utf-8")

    builtin_skills.seed_bundled_skills(manager)
    builtin_skills.seed_bundled_skills(manager)

    assert "Operator wrote this." in _body(dest)
    assert caplog.text.count("customised; not upgraded") == 1
    assert "upgraded bundled skill" not in caplog.text


def test_current_install_is_untouched(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO")
    app_root = tmp_path / "app"
    _make_bundle(app_root, "Same.")
    _history(app_root, "local-pi-delegation", "Same.")
    manager = SkillsManager(str(tmp_path / "data"))
    monkeypatch.setattr(builtin_skills, "get_app_root", lambda: str(app_root))
    builtin_skills.seed_bundled_skills(manager)
    dest = Path(manager.skills_root) / "dev" / "local-pi-delegation"
    (dest / "references" / "marker.md").write_text("keep", encoding="utf-8")

    builtin_skills.seed_bundled_skills(manager)

    assert (dest / "references" / "marker.md").is_file()
    assert "upgraded" not in caplog.text and "customised" not in caplog.text


def test_crlf_and_reserialised_installs_are_not_false_edits(monkeypatch, tmp_path):
    # An old body checked out with CRLF, installed (re-serialised by the
    # seeder), must still match the LF digest in history.
    app_root = tmp_path / "app"
    _make_bundle(app_root, "Old body.\n\nSecond paragraph.")
    skill_file = app_root / "skills" / "local-pi-delegation" / "SKILL.md"
    raw = skill_file.read_bytes().replace(b"\r\n", b"\n")  # write_text is platform dependent
    skill_file.write_bytes(raw.replace(b"\n", b"\r\n"))
    manager = SkillsManager(str(tmp_path / "data"))
    monkeypatch.setattr(builtin_skills, "get_app_root", lambda: str(app_root))
    monkeypatch.setattr(builtin_skills, "_customised_logged", set())
    _history(app_root, "local-pi-delegation", "Old body.\n\nSecond paragraph.", "New body.")
    builtin_skills.seed_bundled_skills(manager)
    dest = Path(manager.skills_root) / "dev" / "local-pi-delegation"

    _make_bundle_files(app_root, "New body.")
    builtin_skills.seed_bundled_skills(manager)

    assert "New body." in _body(dest)


def test_every_bundled_skill_current_body_is_in_history():
    app_root = Path(__file__).resolve().parents[1]
    history = builtin_skills.load_bundled_history(str(app_root))
    for category, name, *_rest in builtin_skills._BUNDLED_SKILLS:
        source = Path(builtin_skills._bundled_source(str(app_root), category, name)) / "SKILL.md"
        digest = builtin_skills.skill_file_digest(str(source))
        assert digest in history.get(name, []), (
            f"{name}: current SKILL.md is not in skills/.bundled-history.json; "
            "run scripts/update_bundled_skill_history.py and commit the result "
            "(otherwise existing installs never upgrade to it)"
        )

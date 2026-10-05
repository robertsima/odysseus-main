import json
from pathlib import Path

from services.memory.skills import SkillsManager
from services.memory.skill_format import Skill
from src import builtin_skills
from tests import REPO_ROOT


def _write_catalog(root: Path, *extra: dict) -> None:
    rows = [{
        "name": "core-sample", "category": "dev", "tier": "core",
        "tags": ["delegation"], "platforms": ["linux", "windows"],
        "requires_toolsets": ["mcp__pi_worker__run_pi_task"],
    }, *extra]
    (root / "skills").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "catalog.json").write_text(
        json.dumps({"version": 1, "skills": rows}), encoding="utf-8")


def _make_bundle(root: Path, body: str = "Bundled instructions.") -> None:
    _write_catalog(root)
    skill_dir = root / "skills" / "core-sample"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: core-sample\n"
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

    destination = Path(manager.skills_root) / "dev" / "core-sample"
    assert installed == ["core-sample"]
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
    )] == ["core-sample"]
    assert manager.index_for(active_toolsets=[]) == []
    assert (destination / "references" / "profile.md").is_file()


def test_seed_bundled_skill_preserves_existing_copy(monkeypatch, tmp_path):
    app_root = tmp_path / "app"
    _make_bundle(app_root, "New bundled content.")
    manager = SkillsManager(str(tmp_path / "data"))
    destination = Path(manager.skills_root) / "dev" / "core-sample"
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

    skill_path = Path(manager.skills_root) / "dev" / "core-sample" / "SKILL.md"
    legacy = Skill.from_markdown(skill_path.read_text(encoding="utf-8"))
    legacy.source = "user"
    legacy.owner = "first-account"
    legacy.body_extra = "Operator-customized delegation instructions."
    skill_path.write_text(legacy.to_markdown(), encoding="utf-8")

    reconciled = builtin_skills.seed_bundled_skills(manager)
    result = Skill.from_markdown(skill_path.read_text(encoding="utf-8"))

    assert reconciled == ["core-sample"]
    assert result.source == "bundled"
    assert result.owner is None
    assert "Operator-customized delegation instructions." in result.body_extra
    assert "New bundled content." not in result.body_extra


_CORE_SKILLS = {"harness-context-and-tool-routing", "visual-asset-sourcing"}
_CURATED_SKILLS = {
    "grilling": "general",
    "writing-for-agents": "general",
    "unslop": "general",
    "diagnosing-bugs": "dev",
    "codebase-design": "dev",
    "domain-modeling": "dev",
    "improve-codebase-architecture": "dev",
    "triage": "dev",
    "resolving-merge-conflicts": "dev",
    "learning-coach": "general",
}
# Community skills carry provenance in their SKILL.md; learning-coach is ours.
_COMMUNITY_SKILLS = {k: v for k, v in _CURATED_SKILLS.items() if k != "learning-coach"}


def test_catalog_tiers_match_the_owner_decision():
    app_root = REPO_ROOT
    catalog = {e["name"]: e for e in builtin_skills.load_catalog(str(app_root))}
    assert {n for n, e in catalog.items() if e["tier"] == "core"} == _CORE_SKILLS
    assert {n for n, e in catalog.items() if e["tier"] == "curated"} == set(_CURATED_SKILLS)
    for name in _CURATED_SKILLS:
        assert catalog[name]["summary"], f"{name} needs a one-line summary for the catalog"


def test_every_catalog_skill_has_a_source_directory():
    app_root = REPO_ROOT
    for entry in builtin_skills.load_catalog(str(app_root)):
        source = builtin_skills._bundled_source(str(app_root), entry["category"], entry["name"])
        assert (Path(source) / "SKILL.md").is_file(), f"{entry['name']} has no SKILL.md"


def test_community_skills_parse_and_are_in_the_curated_catalog():
    app_root = REPO_ROOT
    catalog = {e["name"]: e for e in builtin_skills.load_catalog(str(app_root))}
    for name, category in _COMMUNITY_SKILLS.items():
        assert catalog[name]["tier"] == "curated" and catalog[name]["category"] == category
        assert catalog[name]["license"] == "MIT" and catalog[name]["source"]
        path = app_root / "skills" / category / name / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        skill = Skill.from_markdown(text, path=str(path))
        assert skill.name == name
        assert skill.category == category
        assert skill.status == "published"
        assert skill.description
        # Verified skills must say where their text came from.
        assert "## Provenance" in text and "- License: MIT" in text


def test_community_skills_are_listed_in_acknowledgments():
    app_root = REPO_ROOT
    text = (app_root / "ACKNOWLEDGMENTS.md").read_text(encoding="utf-8")
    assert "## Agent skills" in text
    for name in _COMMUNITY_SKILLS:
        assert f"`{name}" in text


# --- Upgrade of unedited installs (2026-10-01) -------------------------------


def _history(app_root: Path, name: str, *bodies: str) -> None:
    digests = [builtin_skills.skill_digest(_skill_text(b)) for b in bodies]
    (app_root / "skills" / ".bundled-history.json").write_text(
        json.dumps({name: digests}), encoding="utf-8"
    )


def _skill_text(body: str) -> str:
    return (
        "---\nname: core-sample\n"
        "description: Delegate focused work.\n---\n\n" + body
    )


def _setup(monkeypatch, tmp_path, old: str, new: str):
    """Install `old`, then ship `new`; history knows both."""
    app_root = tmp_path / "app"
    _make_bundle(app_root, old)
    manager = SkillsManager(str(tmp_path / "data"))
    monkeypatch.setattr(builtin_skills, "get_app_root", lambda: str(app_root))
    monkeypatch.setattr(builtin_skills, "_customised_logged", set())
    _history(app_root, "core-sample", old, new)
    builtin_skills.seed_bundled_skills(manager)
    _make_bundle_files(app_root, new)
    dest = Path(manager.skills_root) / "dev" / "core-sample"
    return app_root, manager, dest


def _make_bundle_files(app_root: Path, body: str) -> None:
    skill_dir = app_root / "skills" / "core-sample"
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
    assert "upgraded bundled skill core-sample" in caplog.text
    # Upgraded installs count as shipped again, so the tool-gate exemption returns.
    entry = next(s for s in manager.load_all() if s["name"] == "core-sample")
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
    _history(app_root, "core-sample", "Same.")
    manager = SkillsManager(str(tmp_path / "data"))
    monkeypatch.setattr(builtin_skills, "get_app_root", lambda: str(app_root))
    builtin_skills.seed_bundled_skills(manager)
    dest = Path(manager.skills_root) / "dev" / "core-sample"
    (dest / "references" / "marker.md").write_text("keep", encoding="utf-8")

    builtin_skills.seed_bundled_skills(manager)

    assert (dest / "references" / "marker.md").is_file()
    assert "upgraded" not in caplog.text and "customised" not in caplog.text


def test_crlf_and_reserialised_installs_are_not_false_edits(monkeypatch, tmp_path):
    # An old body checked out with CRLF, installed (re-serialised by the
    # seeder), must still match the LF digest in history.
    app_root = tmp_path / "app"
    _make_bundle(app_root, "Old body.\n\nSecond paragraph.")
    skill_file = app_root / "skills" / "core-sample" / "SKILL.md"
    raw = skill_file.read_bytes().replace(b"\r\n", b"\n")  # write_text is platform dependent
    skill_file.write_bytes(raw.replace(b"\n", b"\r\n"))
    manager = SkillsManager(str(tmp_path / "data"))
    monkeypatch.setattr(builtin_skills, "get_app_root", lambda: str(app_root))
    monkeypatch.setattr(builtin_skills, "_customised_logged", set())
    _history(app_root, "core-sample", "Old body.\n\nSecond paragraph.", "New body.")
    builtin_skills.seed_bundled_skills(manager)
    dest = Path(manager.skills_root) / "dev" / "core-sample"

    _make_bundle_files(app_root, "New body.")
    builtin_skills.seed_bundled_skills(manager)

    assert "New body." in _body(dest)


def test_every_shipped_skill_current_body_is_in_history():
    app_root = REPO_ROOT
    history = builtin_skills.load_bundled_history(str(app_root))
    shipped = [(e["name"], builtin_skills.shipped_source_dir(e["name"], str(app_root)))
               for e in builtin_skills.load_catalog(str(app_root))]
    # Integration-owned skills still in the repository (until their package carries them).
    shipped += [(s["name"], s["source_dir"]) for s in builtin_skills._integration_specs(str(app_root))]
    assert len(shipped) >= len(_CORE_SKILLS) + len(_CURATED_SKILLS)
    for name, source_dir in shipped:
        source = Path(source_dir) / "SKILL.md"
        digest = builtin_skills.skill_file_digest(str(source))
        assert digest in history.get(name, []), (
            f"{name}: current SKILL.md is not in skills/.bundled-history.json; "
            "run scripts/update_bundled_skill_history.py and commit the result "
            "(otherwise existing installs never upgrade to it)"
        )


def _integration_skill(root: Path, name: str, body: str) -> Path:
    d = root / "integrations" / "todoist" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Plan.\ncategory: general\n---\n\n{body}",
        encoding="utf-8",
    )
    return d


def test_seeding_is_idempotent_and_survives_preexisting_integration_dir(monkeypatch, tmp_path):
    """2026-10-02: EEXIST on skills/general/todoist-planning aborted all seeding."""
    app_root = tmp_path / "app"
    _make_bundle(app_root)
    src = _integration_skill(app_root, "todoist-planning", "Plan with Todoist.")
    manager = SkillsManager(str(tmp_path / "data"))
    monkeypatch.setattr(builtin_skills, "get_app_root", lambda: str(app_root))
    monkeypatch.setitem(builtin_skills._integration_skill_dirs, "todoist", [str(src.parent)])

    # Older layout: the directory exists but carries a stamp the finder ignores.
    old = Path(manager.skills_root) / "general" / "todoist-planning"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text(
        "---\nname: todoist-planning\ndescription: Plan.\ncategory: general\n"
        "source: user\n---\n\nPlan with Todoist.",
        encoding="utf-8",
    )

    first = builtin_skills.seed_bundled_skills(manager)
    second = builtin_skills.seed_bundled_skills(manager)

    assert "core-sample" in first and "core-sample" in second
    assert "todoist-planning" in first
    adopted = Skill.from_markdown((old / "SKILL.md").read_text(encoding="utf-8"))
    assert adopted.source == "integration"
    assert adopted.requires_integration == "todoist"


def test_one_failing_skill_does_not_stop_the_rest(monkeypatch, tmp_path, caplog):
    app_root = tmp_path / "app"
    _make_bundle(app_root)
    src = _integration_skill(app_root, "todoist-planning", "Plan with Todoist.")
    manager = SkillsManager(str(tmp_path / "data"))
    monkeypatch.setattr(builtin_skills, "get_app_root", lambda: str(app_root))
    monkeypatch.setitem(builtin_skills._integration_skill_dirs, "todoist", [str(src.parent)])
    real = builtin_skills._reconcile_one

    def flaky(sm, **kw):
        if kw["name"] == "core-sample":
            raise OSError(17, "File exists")
        return real(sm, **kw)

    monkeypatch.setattr(builtin_skills, "_reconcile_one", flaky)
    with caplog.at_level("WARNING"):
        installed = builtin_skills.seed_bundled_skills(manager)

    assert installed == ["todoist-planning"]
    assert "core-sample" in caplog.text

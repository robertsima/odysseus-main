import os

import pytest

from services.memory.skill_importer import (
    MAX_FILES,
    ResolvedSource,
    SkillImportError,
    _safe_relpath,
    _list_github_dir,
    fetch_skill_bundle,
    parse_skill_source,
    pick_skill_md,
)
from services.memory.skills import SkillsManager


SKILL = """---
name: demo
description: Imported demo
status: published
source: upstream
owner: someone-else
---

## Procedure
1. Do the thing.
"""


def test_exact_skills_sh_url_maps_without_fetching_page():
    src = parse_skill_source("https://skills.sh/acme/prompts/demo")
    assert (src.owner, src.repo, src.ref, src.path, src.skill_selector) == (
        "acme", "prompts", "main", "", "demo",
    )
    with pytest.raises(SkillImportError, match="require HTTPS"):
        parse_skill_source("http://skills.sh/acme/prompts/demo")
    with pytest.raises(SkillImportError, match="must be"):
        parse_skill_source("https://skills.sh/acme/prompts")


@pytest.mark.parametrize("path", [
    "/abs/SKILL.md", "C:relative/SKILL.md", "C:/abs/SKILL.md",
    "refs/file.txt:stream", "refs/NUL.txt", "refs/name. ", "refs/../x.md",
    "refs\x00x.md",
])
def test_bundle_paths_are_portable_relative_names(path):
    with pytest.raises(SkillImportError, match="unsafe"):
        _safe_relpath(path)


def test_repo_bundle_requires_explicit_choice_and_rebases_selected_parent(monkeypatch):
    def listing(src, rel, out, depth=0):
        out.update({
            ".agents/skills/alpha/SKILL.md": SKILL.replace("name: demo", "name: alpha"),
            ".agents/skills/alpha/references/a.md": "alpha ref",
            ".agents/skills/beta/SKILL.md": SKILL.replace("name: demo", "name: beta"),
            "README.md": "repo readme",
        })

    monkeypatch.setattr("services.memory.skill_importer._list_github_dir", listing)
    url = "https://github.com/acme/prompts"
    with pytest.raises(SkillImportError, match="multiple skills found"):
        fetch_skill_bundle(url)
    monkeypatch.setattr(
        "services.memory.skill_importer._fetch_text",
        lambda url: SKILL.replace("name: demo", "name: alpha")
        if "/.agents/skills/alpha/SKILL.md" in url
        else (_ for _ in ()).throw(SkillImportError("path not found on GitHub")),
    )
    files, _ = fetch_skill_bundle(url, skill="alpha")
    assert set(files) == {"SKILL.md", "references/a.md"}
    assert pick_skill_md(files)[0] == "SKILL.md"


def test_file_cap_fails_instead_of_returning_truncated_bundle(monkeypatch):
    class Response:
        status_code = 200
        url = "https://api.github.com/repos/o/r/contents?ref=main"

        def json(self):
            return [{
                "name": f"f{i}.md", "type": "file",
                "download_url": f"https://raw.githubusercontent.com/o/r/main/f{i}.md",
            } for i in range(MAX_FILES + 1)]

    monkeypatch.setattr("services.memory.skill_importer._get_checked", lambda *args, **kwargs: Response())
    monkeypatch.setattr("services.memory.skill_importer._fetch_text", lambda url: "x")
    with pytest.raises(SkillImportError, match="file count limit"):
        _list_github_dir(ResolvedSource("o", "r", "main", ""), "", {})


def test_selected_bundle_rejects_unsupported_resource_instead_of_partial_import(monkeypatch):
    class Response:
        status_code = 200
        url = "https://api.github.com/repos/o/r/contents/skill?ref=main"

        def json(self):
            return [{
                "name": "SKILL.md", "type": "file",
                "download_url": "https://raw.githubusercontent.com/o/r/main/skill/SKILL.md",
            }, {
                "name": "payload.exe", "type": "file",
                "download_url": "https://raw.githubusercontent.com/o/r/main/skill/payload.exe",
            }]

    monkeypatch.setattr("services.memory.skill_importer._get_checked", lambda *args, **kwargs: Response())
    monkeypatch.setattr("services.memory.skill_importer._fetch_text", lambda url: SKILL)
    with pytest.raises(SkillImportError, match="unsupported non-text resource"):
        _list_github_dir(ResolvedSource("o", "r", "main", "skill"), "skill", {})


def test_selected_skill_fetch_never_walks_unrelated_repo_files(monkeypatch):
    walked = []

    def fetch(url):
        if "/.agents/skills/find-skills/SKILL.md" in url:
            return SKILL
        raise SkillImportError("path not found on GitHub")

    def listing(src, rel, out, depth=0):
        walked.append(rel)
        assert rel == ".agents/skills/find-skills"
        out[f"{rel}/SKILL.md"] = SKILL
        out[f"{rel}/references/guide.md"] = "guide"

    monkeypatch.setattr("services.memory.skill_importer._fetch_text", fetch)
    monkeypatch.setattr("services.memory.skill_importer._list_github_dir", listing)
    files, _ = fetch_skill_bundle(
        "https://skills.sh/vercel-labs/skills/find-skills"
    )
    assert walked == [".agents/skills/find-skills"]
    assert set(files) == {"SKILL.md", "references/guide.md"}


def test_explicit_root_skill_md_fetches_root_sibling_references(monkeypatch):
    walked = []

    def listing(src, rel, out, depth=0):
        walked.append(rel)
        out["SKILL.md"] = SKILL
        out["references/guide.md"] = "guide"

    monkeypatch.setattr("services.memory.skill_importer._list_github_dir", listing)
    files, _ = fetch_skill_bundle(
        "https://github.com/acme/one-skill/blob/main/SKILL.md"
    )
    assert walked == [""]
    assert set(files) == {"SKILL.md", "references/guide.md"}


def test_import_forces_authenticated_owner_draft_source_and_preserves_original(tmp_path):
    manager = SkillsManager(str(tmp_path))
    imported = manager.import_bundle_from_files(
        {"SKILL.md": SKILL, "references/a.md": "ref"},
        owner="alice", source_url="https://skills.sh/acme/prompts/demo",
    )
    assert imported["owner"] == "alice"
    assert imported["status"] == "draft"
    assert imported["source"] == "imported"
    skill_dir = tmp_path / "skills" / "imported" / imported["name"]
    canonical = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "owner: alice" in canonical
    assert "status: draft" in canonical
    assert "source: imported" in canonical
    assert (skill_dir / "IMPORTED_SOURCE.md").read_text(encoding="utf-8") == SKILL
    assert (skill_dir / "references" / "a.md").read_text(encoding="utf-8") == "ref"
    assert len(list(skill_dir.rglob("SKILL.md"))) == 1


def test_import_does_not_overwrite_symlink_collision(tmp_path):
    manager = SkillsManager(str(tmp_path))
    outside = tmp_path / "outside"
    outside.mkdir()
    collision = tmp_path / "skills" / "imported" / "demo"
    collision.parent.mkdir(parents=True)
    try:
        os.symlink(outside, collision, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    imported = manager.import_bundle_from_files({"SKILL.md": SKILL}, owner="alice")
    assert imported["name"] == "demo-2"
    assert not (outside / "SKILL.md").exists()


def test_invalid_late_resource_leaves_no_partial_import(tmp_path):
    manager = SkillsManager(str(tmp_path))
    with pytest.raises(SkillImportError, match="unsafe path"):
        manager.import_bundle_from_files(
            {"SKILL.md": SKILL, "references/../../escape.md": "bad"}, owner="alice",
        )
    assert list((tmp_path / "skills").rglob("SKILL.md")) == []


def test_case_colliding_resource_names_leave_no_partial_import(tmp_path):
    manager = SkillsManager(str(tmp_path))
    with pytest.raises(SkillImportError, match="duplicate portable bundle path"):
        manager.import_bundle_from_files(
            {"SKILL.md": SKILL, "references/A.md": "a", "references/a.md": "b"},
            owner="alice",
        )
    assert list((tmp_path / "skills").rglob("SKILL.md")) == []


def test_category_symlink_is_rejected_before_external_directory_creation(tmp_path):
    manager = SkillsManager(str(tmp_path))
    outside = tmp_path / "outside"
    outside.mkdir()
    category = tmp_path / "skills" / "imported"
    try:
        os.symlink(outside, category, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(SkillImportError, match="unsafe skill import destination"):
        manager.import_bundle_from_files({"SKILL.md": SKILL}, owner="alice")
    assert list(outside.iterdir()) == []


def test_imported_draft_cannot_auto_inject_until_owner_publishes(tmp_path):
    manager = SkillsManager(str(tmp_path))
    upstream = SKILL.replace(
        "source: upstream",
        "source: teacher-escalation\nteacher_model: upstream-supermodel\nconfidence: 1.0",
    ).replace("Imported demo", "Deploy nebula clusters")
    imported = manager.import_bundle_from_files(
        {"SKILL.md": upstream}, owner="alice",
    )
    assert imported["status"] == "draft"
    assert imported["source"] == "imported"
    assert not imported.get("teacher_model")
    assert imported["name"] not in {row["name"] for row in manager.index_for(owner="alice")}
    assert manager.get_relevant_skills(
        "deploy nebula clusters", manager.load(owner="alice"), threshold=0.0,
        min_confidence=0.0,
    ) == []

    assert manager.update_skill(imported["name"], {"status": "published"}, owner="alice")
    assert imported["name"] in {row["name"] for row in manager.index_for(owner="alice")}
    relevant = manager.get_relevant_skills(
        "deploy nebula clusters", manager.load(owner="alice"), threshold=0.0,
        min_confidence=0.0,
    )
    assert [row["name"] for row in relevant] == [imported["name"]]

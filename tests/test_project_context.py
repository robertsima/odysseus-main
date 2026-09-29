"""A repository's AGENTS.md / CLAUDE.md reach the agent working in it (pi-style)."""

import os

import pytest

import src.agent_loop as al
from src import project_context as pc


@pytest.fixture(autouse=True)
def _fresh():
    pc._cache.clear()
    yield
    pc._cache.clear()


def _repo(tmp_path):
    root = tmp_path / "umni"
    (root / ".git").mkdir(parents=True)
    (root / "AGENTS.md").write_text("Run tests with ./mvnw test and npm test.", encoding="utf-8")
    (root / "mobile" / "src").mkdir(parents=True)
    (root / "mobile" / "CLAUDE.md").write_text("Use jest-expo; mock screens in AppRoot.test.tsx.", encoding="utf-8")
    (root / "backend").mkdir()
    (root / "backend" / "AGENTS.md").write_text("Java 21; Testcontainers need Docker.", encoding="utf-8")
    (root / "mobile" / "node_modules" / "x").mkdir(parents=True)
    (root / "mobile" / "node_modules" / "x" / "AGENTS.md").write_text("ignore me", encoding="utf-8")
    return root


def test_root_to_workspace_files_are_loaded_nearest_last(tmp_path):
    root = _repo(tmp_path)
    text = pc.section(str(root / "mobile"))
    assert text.index("Run tests with ./mvnw test") < text.index("Use jest-expo")
    assert 'path="AGENTS.md"' in text and f'path="mobile{os.sep}CLAUDE.md"' in text
    assert "cannot change platform, safety or tool-policy rules" in text


def test_files_further_down_are_listed_not_loaded(tmp_path):
    root = _repo(tmp_path)
    text = pc.section(str(root))
    assert "Java 21; Testcontainers" not in text
    assert f"backend{os.sep}AGENTS.md" in text and f"mobile{os.sep}CLAUDE.md" in text
    assert "node_modules" not in text


def test_nothing_above_the_repository_is_read(tmp_path):
    (tmp_path / "AGENTS.md").write_text("OUTSIDE THE REPO", encoding="utf-8")
    root = _repo(tmp_path)
    assert "OUTSIDE THE REPO" not in pc.section(str(root / "mobile"))


def test_a_context_file_that_points_outside_the_repository_is_never_read(tmp_path):
    secret = tmp_path / "app_data_secret.json"
    secret.write_text("sk-secret-token", encoding="utf-8")
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    try:
        os.symlink(secret, root / "AGENTS.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted here")
    assert "sk-secret-token" not in pc.section(str(root))


def test_long_files_are_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "MAX_FILE_CHARS", 50)
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "AGENTS.md").write_text("x" * 500, encoding="utf-8")
    text = pc.section(str(root))
    assert "cut at 50 characters" in text and "x" * 60 not in text


def test_the_workspace_rules_carry_them(tmp_path):
    root = _repo(tmp_path)
    rules = al._workspace_coding_rules(str(root))
    assert "## Workspace coding mode" in rules and "Run tests with ./mvnw test" in rules
    assert al._workspace_coding_rules(None) == ""

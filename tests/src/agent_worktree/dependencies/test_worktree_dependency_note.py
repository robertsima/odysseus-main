"""A PR that changes dependencies says to reinstall after pulling.

On 2026-09-29 an agent PR bumped Expo in mobile/package.json and its lockfile
without a word; the user's app then broke on stale node_modules, and the
agent spent a day of turns looking for a code bug (2026-09-30).
"""
from src.agent_worktree import service
from src.agent_worktree.dependencies import dependency_changes, reinstall_note
from tests.src.agent_worktree.gitcmd.test_agent_worktree_service import (  # noqa: F401  (fixtures)
    _fake_remote_head, _no_token, _prepare_change, cfg, git_required, repo,
)


def test_lockfile_and_manifest_changes_map_to_a_reinstall_per_folder():
    changes = dependency_changes(["mobile/package.json", "mobile/package-lock.json", "backend/pom.xml",
                                  "backend/src/App.java", "README.md", "gradle/libs.versions.toml"])
    assert changes == [
        {"folder": "mobile", "kind": "npm", "command": "npm ci", "file": "mobile/package-lock.json"},
        {"folder": "backend", "kind": "maven",
         "command": "./mvnw -U clean install (or reload the Maven project)", "file": "backend/pom.xml"},
        {"folder": ".", "kind": "gradle",
         "command": "./gradlew --refresh-dependencies build (or sync the Gradle project)",
         "file": "gradle/libs.versions.toml"},
    ]
    note = reinstall_note(changes)
    assert note.startswith("### Dependencies changed")
    assert "in `mobile/`: `npm ci`" in note and "the repository root" in note


def test_a_script_only_package_json_edit_is_not_flagged():
    assert dependency_changes(["mobile/package.json", "mobile/src/App.tsx"]) == []
    assert reinstall_note([]) == ""


@git_required
async def test_request_publish_puts_the_note_in_the_pr_and_the_result(cfg, monkeypatch):
    await _prepare_change(cfg, filename="mobile/package-lock.json", body="{}")
    _no_token(monkeypatch, cfg)
    monkeypatch.setattr(service, "_remote_head", _fake_remote_head(None))

    view = await service.request_publish("task", title="Bump expo", body="Updates Expo.", cfg=cfg)

    assert view["body"].startswith("Updates Expo.")
    assert "### Dependencies changed" in view["body"] and "`npm ci`" in view["body"]
    assert view["dependency_changes"][0]["command"] == "npm ci"
    assert "npm ci" in view["dependency_note"]


@git_required
async def test_a_change_without_dependency_files_gets_no_note(cfg, monkeypatch):
    await _prepare_change(cfg)
    _no_token(monkeypatch, cfg)
    monkeypatch.setattr(service, "_remote_head", _fake_remote_head(None))

    view = await service.request_publish("task", title="Add feature", body="Adds it.", cfg=cfg)

    assert view["body"] == "Adds it."
    assert "dependency_note" not in view


def test_coding_rules_cover_where_the_app_runs_and_stale_installs():
    from src.agent_loop import _workspace_coding_rules

    rules = _workspace_coding_rules("/app/data/development/dog-trainer")
    for phrase in ("may live on another machine", "lockfile", "`npm ci`",
                   "browser console", "alters dependency versions"):
        assert phrase in rules, phrase

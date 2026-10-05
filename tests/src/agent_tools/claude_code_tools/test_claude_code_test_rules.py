"""Delegated Claude Code can run the checkout's own tests (claude_code_tools.project_test_rules).

On 2026-09-30 two Opus runs could not test anything: the first had no test
runner granted ("every command that runs something was denied
automatically"); the second had `./mvnw test`, but the wrapper lives in
`backend/`, Claude Code starts at the checkout's root and may not `cd`.
"""
import json

import pytest

from src.agent_tools import claude_code_tools as cct


@pytest.fixture
def monorepo(tmp_path, monkeypatch):
    dev = tmp_path / "development"
    repo = dev / "umni"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / "backend").mkdir()
    (repo / "backend" / "pom.xml").write_text("<project/>", encoding="utf-8")
    (repo / "backend" / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "mobile").mkdir()
    (repo / "mobile" / "package.json").write_text(json.dumps(
        {"scripts": {"test": "jest", "typecheck": "tsc --noEmit", "start": "expo start"}}), encoding="utf-8")
    (repo / "node_modules" / "x").mkdir(parents=True)
    (repo / "node_modules" / "x" / "package.json").write_text('{"scripts": {"test": "x"}}', encoding="utf-8")
    monkeypatch.setattr(cct, "DEFAULT_ROOTS", (str(dev),))
    monkeypatch.setattr(cct, "DEFAULT_REPOSITORY", "")
    monkeypatch.delenv("ODYSSEUS_AGENT_SOURCE_REPO", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ODYSSEUS_URL", raising=False)
    return repo


def test_subfolder_projects_get_rules_that_work_from_the_root(monorepo):
    rules, commands = cct.project_test_rules(monorepo)
    for rule in ("Bash(cd backend)", "Bash(./mvnw test:*)", "Bash(./backend/mvnw -f backend/pom.xml test:*)",
                 "Bash(mvn -f backend/pom.xml verify:*)", "Bash(cd mobile)", "Bash(npm test:*)",
                 "Bash(npm --prefix mobile test:*)", "Bash(npm --prefix mobile run typecheck:*)"):
        assert rule in rules, rule
    assert all(cct.SAFE_TOOL.fullmatch(rule) for rule in rules)
    # Only test/build scripts, and nothing from dependency folders.
    assert not any("start" in rule or "node_modules" in rule for rule in rules)
    assert "cd backend && ./mvnw test" in commands and "npm --prefix mobile test" in commands


def test_the_allowlist_still_refuses_anything_else():
    for unsafe in ("Bash(cd ../etc)", "Bash(cd /)", "Bash(cd backend && rm -rf x)", "Bash(cd backend:*)",
                   "Bash(npm --prefix ../x test:*)", "Bash(mvn -f ../pom.xml test:*)",
                   "Bash(npm --prefix mobile run start:*)", "Bash(./backend/mvnw -f backend/pom.xml deploy:*)",
                   "Bash(cd back end)", "Bash(cd $(whoami))"):
        assert not cct.SAFE_TOOL.fullmatch(unsafe), unsafe


def test_every_delegation_gets_them_and_is_told_which(monorepo):
    out = cct._parse_args({"repository": str(monorepo), "prompt": "Implement the endpoint."})
    assert "error" not in out
    assert set(cct.default_tools()) <= set(out["tools"])
    assert "Bash(cd backend)" in out["tools"] and "Bash(npm --prefix mobile test:*)" in out["tools"]
    assert out["prompt"].startswith("Implement the endpoint.")
    assert "`cd backend && ./mvnw test`" in out["prompt"]


def test_the_setting_turns_it_off(monorepo, monkeypatch):
    monkeypatch.setattr(cct, "_setting", lambda key, default=None: False if key == "claude_code_test_commands" else default)
    out = cct._parse_args({"repository": str(monorepo), "prompt": "x"})
    assert out["tools"] == cct.default_tools()
    assert out["prompt"] == "x"

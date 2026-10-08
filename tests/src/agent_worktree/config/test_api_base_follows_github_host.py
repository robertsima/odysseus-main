"""Gated publishing talks to the same GitHub host as the MCP servers and git.

GITHUB_HOST already pointed the GitHub MCP servers and the git credential at a
GitHub Enterprise install, but publishing read its own ODYSSEUS_GITHUB_API_BASE
and otherwise used api.github.com. An Enterprise install that set only
GITHUB_HOST pushed to its own host and then opened the pull request against
github.com, which answered 404.
"""
import pytest

from src.agent_worktree.config import load_config


@pytest.mark.parametrize("host, api", [
    ("", "https://api.github.com"),
    ("github.com", "https://api.github.com"),
    ("https://github.example.com", "https://github.example.com/api/v3"),
    ("github.example.com", "https://github.example.com/api/v3"),
    ("acme.ghe.com", "https://api.acme.ghe.com"),
])
def test_api_base_follows_github_host(monkeypatch, host, api):
    monkeypatch.delenv("ODYSSEUS_GITHUB_API_BASE", raising=False)
    monkeypatch.setenv("GITHUB_HOST", host)
    assert load_config().api_base == api


def test_an_explicit_api_base_still_wins(monkeypatch):
    monkeypatch.setenv("GITHUB_HOST", "github.example.com")
    monkeypatch.setenv("ODYSSEUS_GITHUB_API_BASE", "https://gh-proxy.example.com/api/v3/")
    assert load_config().api_base == "https://gh-proxy.example.com/api/v3"

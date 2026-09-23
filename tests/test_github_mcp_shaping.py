"""GitHub MCP list/search results are kept small enough to stay inline.

Production logs showed `search_issues` returning 105k-203k characters and
`list_pull_requests` up to 270k: no page size, and every item a raw REST object
with its full body. These tests pin the two fixes -- a default page size (only
when the tool's schema declares one and the model gave none) and per-item
compaction -- and that anything unrecognised passes through untouched.
"""

import json
from types import SimpleNamespace

import pytest

from src import github_mcp_shaping as shaping
from src.github_mcp_shaping import (
    BODY_MAX_CHARS,
    DEFAULT_PER_PAGE,
    compact_github_mcp_result,
    github_mcp_request_defaults,
)

LONG_BODY = "Steps to reproduce:\n" + ("The agent loops on recall_tool_output. " * 120)


def _user(login):
    base = f"https://api.github.com/users/{login}"
    return {
        "login": login,
        "id": 101,
        "node_id": "MDQ6VXNlcjEwMQ==",
        "avatar_url": f"https://avatars.githubusercontent.com/u/101?v=4",
        "gravatar_id": "",
        "url": base,
        "html_url": f"https://github.com/{login}",
        "followers_url": f"{base}/followers",
        "following_url": f"{base}/following{{/other_user}}",
        "gists_url": f"{base}/gists{{/gist_id}}",
        "starred_url": f"{base}/starred{{/owner}}{{/repo}}",
        "subscriptions_url": f"{base}/subscriptions",
        "organizations_url": f"{base}/orgs",
        "repos_url": f"{base}/repos",
        "events_url": f"{base}/events{{/privacy}}",
        "received_events_url": f"{base}/received_events",
        "type": "User",
        "site_admin": False,
    }


def _label(name):
    return {
        "id": 42,
        "node_id": "LA_kwDOabc",
        "url": f"https://api.github.com/repos/o/r/labels/{name}",
        "name": name,
        "color": "d73a4a",
        "default": False,
        "description": f"Something about {name}",
    }


def _reactions(url):
    return {"url": url, "total_count": 3, "+1": 2, "-1": 0, "laugh": 0,
            "hooray": 0, "confused": 0, "heart": 1, "rocket": 0, "eyes": 0}


def _issue(number, *, pr=False):
    api = f"https://api.github.com/repos/o/r/issues/{number}"
    item = {
        "url": api,
        "repository_url": "https://api.github.com/repos/o/r",
        "labels_url": f"{api}/labels{{/name}}",
        "comments_url": f"{api}/comments",
        "events_url": f"{api}/events",
        "html_url": f"https://github.com/o/r/{'pull' if pr else 'issues'}/{number}",
        "id": 9000 + number,
        "node_id": "I_kwDOabc",
        "number": number,
        "title": f"Sub-agent offloads every GitHub search #{number}",
        "user": _user("octocat"),
        "labels": [_label("bug"), _label("agent")],
        "state": "open",
        "locked": False,
        "assignee": _user("hubot"),
        "assignees": [_user("hubot")],
        "milestone": None,
        "comments": 4,
        "created_at": "2026-09-01T10:00:00Z",
        "updated_at": "2026-09-20T10:00:00Z",
        "closed_at": None,
        "author_association": "OWNER",
        "active_lock_reason": None,
        "body": LONG_BODY,
        "reactions": _reactions(f"{api}/reactions"),
        "timeline_url": f"{api}/timeline",
        "performed_via_github_app": None,
        "state_reason": None,
        "score": 1.0,
    }
    if pr:
        item["draft"] = False
        item["pull_request"] = {
            "url": f"https://api.github.com/repos/o/r/pulls/{number}",
            "html_url": f"https://github.com/o/r/pull/{number}",
            "diff_url": f"https://github.com/o/r/pull/{number}.diff",
            "patch_url": f"https://github.com/o/r/pull/{number}.patch",
            "merged_at": None,
        }
    return item


def _repo():
    return {
        "id": 7, "node_id": "R_kgDO", "name": "r", "full_name": "o/r",
        "private": False, "owner": _user("o"), "html_url": "https://github.com/o/r",
        "description": "A repository", "fork": False,
        "url": "https://api.github.com/repos/o/r",
        **{f"{k}_url": f"https://api.github.com/repos/o/r/{k}" for k in (
            "forks", "keys", "collaborators", "teams", "hooks", "issue_events",
            "events", "assignees", "branches", "tags", "blobs", "git_tags",
            "git_refs", "trees", "statuses", "languages", "stargazers",
            "contributors", "subscribers", "subscription", "commits",
            "git_commits", "comments", "issue_comment", "contents", "compare",
            "merges", "archive", "downloads", "issues", "pulls", "milestones",
            "notifications", "labels", "releases", "deployments")},
    }


def _pull(number):
    api = f"https://api.github.com/repos/o/r/pulls/{number}"
    return {
        "url": api, "id": 5000 + number, "node_id": "PR_kwDO",
        "html_url": f"https://github.com/o/r/pull/{number}",
        "diff_url": f"https://github.com/o/r/pull/{number}.diff",
        "patch_url": f"https://github.com/o/r/pull/{number}.patch",
        "issue_url": f"https://api.github.com/repos/o/r/issues/{number}",
        "number": number, "state": "open", "locked": False,
        "title": f"Compact GitHub MCP results #{number}",
        "user": _user("octocat"), "body": LONG_BODY,
        "created_at": "2026-09-10T10:00:00Z", "updated_at": "2026-09-11T10:00:00Z",
        "closed_at": None, "merged_at": None,
        "merge_commit_sha": "abc123", "assignee": None, "assignees": [],
        "requested_reviewers": [_user("reviewer")], "requested_teams": [],
        "labels": [_label("enhancement")], "milestone": None, "draft": True,
        "commits_url": f"{api}/commits", "review_comments_url": f"{api}/comments",
        "review_comment_url": "https://api.github.com/repos/o/r/pulls/comments{/number}",
        "comments_url": f"https://api.github.com/repos/o/r/issues/{number}/comments",
        "statuses_url": "https://api.github.com/repos/o/r/statuses/abc",
        "head": {"label": "o:feature", "ref": f"feature-{number}", "sha": "def456",
                 "user": _user("o"), "repo": _repo()},
        "base": {"label": "o:dev", "ref": "dev", "sha": "0a1b2c",
                 "user": _user("o"), "repo": _repo()},
        "_links": {k: {"href": f"{api}/{k}"} for k in (
            "self", "html", "issue", "comments", "review_comments",
            "review_comment", "commits", "statuses")},
        "author_association": "OWNER", "auto_merge": None, "active_lock_reason": None,
    }


def _commit(i):
    sha = f"{i:040x}"
    return {
        "sha": sha, "node_id": "C_kwDO",
        "commit": {
            "author": {"name": "Octo Cat", "email": "octo@example.com", "date": "2026-09-20T12:00:00Z"},
            "committer": {"name": "GitHub", "email": "noreply@github.com", "date": "2026-09-20T12:00:00Z"},
            "message": f"Trim GitHub MCP results ({i})\n\nLonger explanation that nobody needs in a list.\n",
            "tree": {"sha": "t" * 40, "url": "https://api.github.com/repos/o/r/git/trees/x"},
            "url": f"https://api.github.com/repos/o/r/git/commits/{sha}",
            "comment_count": 0,
            "verification": {"verified": True, "reason": "valid", "signature": "-----BEGIN PGP SIGNATURE-----\n" + "x" * 800, "payload": "tree x\n" * 20},
        },
        "url": f"https://api.github.com/repos/o/r/commits/{sha}",
        "html_url": f"https://github.com/o/r/commit/{sha}",
        "comments_url": f"https://api.github.com/repos/o/r/commits/{sha}/comments",
        "author": _user("octocat"), "committer": _user("web-flow"),
        "parents": [{"sha": "p" * 40, "url": "https://api.github.com/x", "html_url": "https://github.com/x"}],
    }


def _code_hit(i):
    return {
        "name": f"module_{i}.py", "path": f"src/module_{i}.py", "sha": "c" * 40,
        "url": f"https://api.github.com/repos/o/r/contents/src/module_{i}.py?ref=abc",
        "git_url": "https://api.github.com/repos/o/r/git/blobs/ccc",
        "html_url": f"https://github.com/o/r/blob/abc/src/module_{i}.py",
        "repository": _repo(), "score": 1.0,
    }


def _ok(payload):
    return {"stdout": json.dumps(payload), "stderr": "", "exit_code": 0}


class _SchemaMcp:
    def __init__(self, properties):
        self.properties = properties

    def get_tool_input_schema(self, qualified):
        if self.properties is None:
            return None
        return {"type": "object", "properties": self.properties}


# ── Request defaults ──

def test_per_page_is_injected_when_declared_and_absent():
    mcp = _SchemaMcp({"query": {"type": "string"}, "perPage": {"type": "number"}})
    args, injected = github_mcp_request_defaults(
        "mcp__github_read__search_issues", {"query": "offload"}, mcp
    )
    assert args == {"query": "offload", "perPage": DEFAULT_PER_PAGE}
    assert injected == DEFAULT_PER_PAGE


@pytest.mark.parametrize("supplied", [{"perPage": 50}, {"per_page": 3}])
def test_model_supplied_page_size_is_kept(supplied):
    mcp = _SchemaMcp({"perPage": {"type": "number"}})
    original = {"owner": "o", "repo": "r", **supplied}
    args, injected = github_mcp_request_defaults(
        "mcp__github_read__list_pull_requests", original, mcp
    )
    assert args == original
    assert injected is None


def test_nothing_injected_when_schema_lacks_per_page_or_is_unknown():
    args = {"owner": "o", "repo": "r"}
    for mcp in (_SchemaMcp({"owner": {}, "repo": {}}), _SchemaMcp(None), object()):
        shaped, injected = github_mcp_request_defaults(
            "mcp__github_read__list_commits", args, mcp
        )
        assert shaped == {"owner": "o", "repo": "r"}
        assert injected is None


def test_only_list_and_search_tools_get_defaults():
    mcp = _SchemaMcp({"perPage": {}, "minimal_output": {"type": "boolean"}})
    for tool in ("mcp__github_read__issue_read", "mcp__github_read__get_file_contents",
                 "mcp__firecrawl__search_issues", "mcp__github_write__create_pull_request"):
        assert github_mcp_request_defaults(tool, {"x": 1}, mcp) == ({"x": 1}, None)


def test_minimal_output_set_when_declared_and_kept_when_supplied():
    mcp = _SchemaMcp({"perPage": {}, "minimal_output": {"type": "boolean"}})
    args, _ = github_mcp_request_defaults("mcp__github__list_issues", {"owner": "o"}, mcp)
    assert args["minimal_output"] is True

    args, _ = github_mcp_request_defaults(
        "mcp__github__list_issues", {"owner": "o", "minimal_output": False}, mcp
    )
    assert args["minimal_output"] is False

    args, _ = github_mcp_request_defaults(
        "mcp__github__list_issues", {"owner": "o"}, _SchemaMcp({"perPage": {}})
    )
    assert "minimal_output" not in args


def test_schema_lookup_error_injects_nothing():
    class Broken:
        def get_tool_input_schema(self, qualified):
            raise RuntimeError("boom")

    args = {"query": "x"}
    assert github_mcp_request_defaults("mcp__github__search_code", args, Broken()) == (args, None)


# ── Result compaction ──

def test_search_issues_is_compacted_and_keeps_key_fields(caplog):
    payload = {"total_count": 57, "incomplete_results": False,
               "items": [_issue(n, pr=(n % 2 == 0)) for n in range(1, 11)]}
    original = _ok(payload)
    with caplog.at_level("INFO", logger="src.github_mcp_shaping"):
        shaped = compact_github_mcp_result("mcp__github_read__search_issues", original)

    before, after = len(original["stdout"]), len(shaped["stdout"])
    assert after < before / 4
    assert any("search_issues" in r.message and f"{before} -> {after}" in r.message
               for r in caplog.records)

    data = json.loads(shaped["stdout"])
    assert data["total_count"] == 57 and data["incomplete_results"] is False
    first, second = data["items"][0], data["items"][1]
    assert first["number"] == 1
    assert first["title"].startswith("Sub-agent offloads")
    assert first["state"] == "open"
    assert first["html_url"] == "https://github.com/o/r/issues/1"
    assert first["labels"] == ["bug", "agent"]
    assert first["user"] == "octocat"
    assert first["assignees"] == ["hubot"]
    assert first["comments"] == 4
    assert "is_pull_request" not in first and second["is_pull_request"] is True
    assert first["body"].startswith(LONG_BODY[:BODY_MAX_CHARS])
    assert f"{len(LONG_BODY) - BODY_MAX_CHARS} more chars cut" in first["body"]
    assert "issue_read" in first["body"] and "pull_request_read" in second["body"]
    for dropped in ("node_id", "reactions", "repository_url", "comments_url",
                    "performed_via_github_app", "author_association"):
        assert dropped not in first
    # The result dict keeps the shape format_tool_result expects.
    assert shaped["exit_code"] == 0 and shaped["stderr"] == ""
    assert original["stdout"] == json.dumps(payload)  # input not mutated


def test_list_pull_requests_is_compacted():
    original = _ok([_pull(n) for n in range(1, 6)])
    shaped = compact_github_mcp_result("mcp__github_read__list_pull_requests", original)
    assert len(shaped["stdout"]) < len(original["stdout"]) / 8
    pr = json.loads(shaped["stdout"])[0]
    assert pr["number"] == 1 and pr["state"] == "open" and pr["draft"] is True
    assert pr["html_url"] == "https://github.com/o/r/pull/1"
    assert pr["labels"] == ["enhancement"]
    assert pr["head"] == "feature-1" and pr["base"] == "dev"
    assert "pull_request_read" in pr["body"]
    assert "_links" not in pr and "requested_reviewers" not in pr


def test_list_commits_is_compacted():
    original = _ok([_commit(i) for i in range(5)])
    shaped = compact_github_mcp_result("mcp__github_read__list_commits", original)
    assert len(shaped["stdout"]) < len(original["stdout"]) / 8
    commit = json.loads(shaped["stdout"])[0]
    assert commit == {
        "sha": f"{0:040x}",
        "message": "Trim GitHub MCP results (0)",
        "author": "octocat",
        "date": "2026-09-20T12:00:00Z",
        "html_url": f"https://github.com/o/r/commit/{0:040x}",
    }


def test_search_code_is_compacted():
    original = _ok({"total_count": 3, "incomplete_results": False,
                    "items": [_code_hit(i) for i in range(3)]})
    shaped = compact_github_mcp_result("mcp__github_read__search_code", original)
    assert len(shaped["stdout"]) < len(original["stdout"]) / 10
    data = json.loads(shaped["stdout"])
    assert data["total_count"] == 3
    assert data["items"][0] == {
        "path": "src/module_0.py",
        "repository": "o/r",
        "html_url": "https://github.com/o/r/blob/abc/src/module_0.py",
    }


def test_issue_comments_are_compacted_but_bodies_kept_whole():
    comments = [{
        "id": 77, "node_id": "IC_kw", "url": "https://api.github.com/x",
        "html_url": "https://github.com/o/r/issues/1#issuecomment-77",
        "issue_url": "https://api.github.com/repos/o/r/issues/1",
        "user": _user("octocat"), "created_at": "2026-09-02T00:00:00Z",
        "updated_at": "2026-09-02T00:00:00Z", "author_association": "OWNER",
        "body": LONG_BODY, "reactions": _reactions("https://api.github.com/r"),
        "performed_via_github_app": None,
    }]
    original = _ok(comments)
    shaped = compact_github_mcp_result("mcp__github_read__issue_read", original)
    comment = json.loads(shaped["stdout"])[0]
    assert comment["body"] == LONG_BODY
    assert comment["user"] == "octocat" and comment["id"] == 77
    assert "reactions" not in comment and "node_id" not in comment

    # issue_read "get" returns the issue itself, which stays verbatim.
    single = _ok(_issue(1))
    assert compact_github_mcp_result("mcp__github_read__issue_read", single) is single


def test_injected_page_size_is_announced_when_the_page_is_full():
    original = _ok([_pull(n) for n in range(1, DEFAULT_PER_PAGE + 1)])
    shaped = compact_github_mcp_result(
        "mcp__github_read__list_pull_requests", original, DEFAULT_PER_PAGE
    )
    json_text, note = shaped["stdout"].rsplit("\n", 1)
    assert len(json.loads(json_text)) == DEFAULT_PER_PAGE
    assert "no perPage was given" in note

    short = _ok([_pull(1)])
    assert "perPage" not in compact_github_mcp_result(
        "mcp__github_read__list_pull_requests", short, DEFAULT_PER_PAGE
    )["stdout"]


@pytest.mark.parametrize("result", [
    {"stdout": "No issues matched your query.", "stderr": "", "exit_code": 0},
    {"stdout": "", "stderr": "API rate limit exceeded", "exit_code": 1},
    {"stdout": json.dumps([{"filename": "a.py", "patch": "@@"}]), "stderr": "", "exit_code": 0},
    {"stdout": json.dumps({"weird": [1, 2]}), "stderr": "", "exit_code": 0},
    {"stdout": "[]", "stderr": "", "exit_code": 0},
    {"error": "MCP server not connected: github_read", "exit_code": 1},
])
def test_non_json_errors_and_unknown_shapes_pass_through(result):
    assert compact_github_mcp_result("mcp__github_read__search_issues", result) is result


def test_unknown_tools_pass_through():
    result = _ok({"items": [_issue(1)]})
    for tool in ("mcp__github_read__get_file_contents", "mcp__other__search_issues",
                 "read_file"):
        assert compact_github_mcp_result(tool, result) is result


def test_errors_fall_back_to_the_original(monkeypatch):
    def explode(item):
        raise ValueError("unexpected")

    monkeypatch.setattr(shaping, "_compact_issue", explode)
    result = _ok({"items": [_issue(1)]})
    assert compact_github_mcp_result("mcp__github_read__search_issues", result) is result


# ── Dispatch ──

@pytest.mark.asyncio
async def test_mcp_dispatch_injects_page_size_and_compacts(monkeypatch):
    import src.tool_execution as tool_execution
    from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block

    class FakeMcp:
        def __init__(self):
            self.calls = []

        def get_tool_input_schema(self, qualified):
            return {"type": "object", "properties": {"query": {}, "perPage": {}}}

        async def call_tool(self, name, args):
            self.calls.append((name, args))
            return _ok({"total_count": 1, "incomplete_results": False, "items": [_issue(1)]})

    fake = FakeMcp()
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: fake)

    _desc, result = await execute_tool_block(
        SimpleNamespace(tool_type="mcp__github_read__search_issues",
                        content='{"query": "offload"}'),
        owner="admin-user",
        security_context=NO_TOOL_SECURITY_CONTEXT,
    )
    assert fake.calls == [
        ("mcp__github_read__search_issues", {"query": "offload", "perPage": DEFAULT_PER_PAGE}),
    ]
    assert "reactions" not in result["stdout"]
    assert json.loads(result["stdout"])["items"][0]["number"] == 1


@pytest.mark.asyncio
async def test_mcp_manager_exposes_advertised_schema():
    from src.mcp_manager import McpManager

    manager = McpManager()
    schema = {"type": "object", "properties": {"perPage": {"type": "number"}}}
    manager._tools["github_read"] = [{"name": "search_issues", "input_schema": schema}]
    assert manager.get_tool_input_schema("mcp__github_read__search_issues") == schema
    assert manager.get_tool_input_schema("mcp__github_read__list_commits") is None
    assert manager.get_tool_input_schema("search_issues") is None

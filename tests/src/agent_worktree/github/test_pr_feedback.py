"""PR feedback through the worktree's GitHub credential: reduced payloads,
validated writes, token never in errors."""
import pytest

from src.agent_worktree import github as gh
from src.agent_worktree.config import WorktreeConfig


def _cfg(slug="acme/odysseus", app=True):
    return WorktreeConfig(
        publish_enabled=False, repo_slug=slug, source_repo="/tmp/src", worktree_root="/tmp/wt",
        state_dir="/tmp/state", base_branch="dev", approval_ttl_s=900, api_base="https://api.github.com",
        app_id="1" if app else "", installation_id="2" if app else "", private_key_path="/nope.pem" if app else "",
        fallback_token_env="ODYSSEUS_AGENT_GITHUB_TOKEN", _fallback_token_present=not app,
    )


@pytest.fixture
def api(monkeypatch):
    calls = []
    responses = {}

    async def fake_api(cfg, token, method, path, *, params=None, json_body=None, accept="application/vnd.github+json"):
        calls.append((method, path, params, json_body, accept))
        key = (method, path.split("?")[0])
        if key in responses:
            return responses[key]
        return [] if method == "GET" else {"id": 1, "html_url": "https://github.com/x"}

    monkeypatch.setattr(gh, "_api", fake_api)
    return calls, responses


def test_access_blockers_do_not_require_the_publish_flag(monkeypatch, tmp_path):
    key = tmp_path / "app.pem"
    key.write_text("k")
    cfg = _cfg()
    cfg = WorktreeConfig(**{**cfg.__dict__, "private_key_path": str(key)})
    assert gh.pr_access_blockers(cfg) == []
    assert any("slug" in b for b in gh.pr_access_blockers(_cfg(slug="")))
    assert any("does not point at a file" in b for b in gh.pr_access_blockers(_cfg()))


async def test_list_and_detail_reduce_github_payloads(api):
    calls, responses = api
    cfg = _cfg()
    responses[("GET", "/repos/acme/odysseus/pulls")] = [{
        "number": 7, "title": "Fix", "state": "open", "draft": True, "html_url": "u", "user": {"login": "bob"},
        "head": {"ref": "agent/odysseus/fix", "sha": "abc"}, "base": {"ref": "dev"}, "labels": [{"name": "bug"}],
        "body": "x" * 1000, "secret_field": "should not pass through",
    }]
    rows = await gh.list_pull_requests(cfg, "tok", state="open", limit=5)
    assert rows[0]["number"] == 7 and rows[0]["user"] == "bob" and rows[0]["labels"] == ["bug"]
    assert len(rows[0]["body_excerpt"]) == 400 and "secret_field" not in rows[0]
    assert calls[0][2]["per_page"] == 5

    responses[("GET", "/repos/acme/odysseus/pulls/7")] = {"number": 7, "title": "Fix", "body": "long", "mergeable": True,
                                                          "head": {"sha": "abc"}, "base": {}, "changed_files": 2}
    detail = await gh.get_pull_request(cfg, "tok", 7)
    assert detail["mergeable"] is True and detail["changed_files"] == 2 and detail["body"] == "long"

    responses[("GET", "/repos/acme/odysseus/pulls/7/files")] = [{"filename": "a.py", "status": "modified",
                                                                "additions": 1, "deletions": 2, "patch": "p" * 30000}]
    files = await gh.pull_request_files(cfg, "tok", 7)
    assert files[0]["patch_truncated"] is True and len(files[0]["patch"]) == gh.MAX_PATCH_CHARS

    assert await gh.pull_request_checks(cfg, "tok", "not a sha!") == []
    responses[("GET", "/repos/acme/odysseus/commits/abc/check-runs")] = {"check_runs": [
        {"name": "tests", "status": "completed", "conclusion": "success", "html_url": "c", "app": {"slug": "gha"}}]}
    checks = await gh.pull_request_checks(cfg, "tok", "abc")
    assert checks[0]["conclusion"] == "success" and checks[0]["app"] == "gha"


async def test_review_validation_and_payload(api):
    calls, _ = api
    cfg = _cfg()
    with pytest.raises(gh.GitHubError, match="must be one of"):
        await gh.create_review(cfg, "tok", 7, event="MERGE_IT")
    with pytest.raises(gh.GitHubError, match="required"):
        await gh.create_review(cfg, "tok", 7, event="REQUEST_CHANGES", body="  ")
    with pytest.raises(gh.GitHubError, match="required"):
        await gh.create_issue_comment(cfg, "tok", 7, "")

    out = await gh.create_review(cfg, "tok", 7, event="request_changes", body="please fix",
                                 comments=[{"path": "a.py", "line": 3, "body": "typo", "side": "right"},
                                           {"path": "", "line": 1, "body": "dropped"},
                                           {"path": "b.py", "line": "x", "body": "dropped too"}])
    assert out["id"] == 1
    method, path, _, payload, _ = calls[-1]
    assert (method, path) == ("POST", "/repos/acme/odysseus/pulls/7/reviews")
    assert payload["event"] == "REQUEST_CHANGES" and payload["body"] == "please fix"
    assert payload["comments"] == [{"path": "a.py", "line": 3, "body": "typo", "side": "RIGHT"}]

    approve = await gh.create_review(cfg, "tok", 7, event="APPROVE")
    assert approve["id"] == 1 and "comments" not in calls[-1][3]


async def test_api_errors_are_scrubbed_of_the_token(monkeypatch):
    class _Resp:
        status_code = 403
        text = "forbidden for token ghs_secretvalue123"

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **k):
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(gh.GitHubError) as exc:
        await gh.list_pull_requests(_cfg(), "ghs_secretvalue123")
    assert "ghs_secretvalue123" not in str(exc.value) and "403" in str(exc.value)

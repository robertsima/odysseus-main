"""Fail-closed configuration, validation, and sensitive-path classification.

These cover the decisions that happen before any git or network call: whether
publishing is permitted at all, whether a ref the model supplied is safe to hand
to git, and whether a change needs the operator's second acknowledgement.
"""

import os

import pytest

from src.agent_worktree import sensitive
from src.agent_worktree.config import BRANCH_PREFIX, load_config, publish_blockers
from src.agent_worktree.validation import (
    branch_leaf_from_name,
    is_valid_repo_slug,
    is_valid_sha,
    normalize_branch,
    safe_worktree_path,
    validate_agent_branch,
)

pytestmark = pytest.mark.area_security


_PUBLISH_ENV = (
    "ODYSSEUS_AGENT_PUBLISH_ENABLED",
    "ODYSSEUS_AGENT_REPO",
    "ODYSSEUS_AGENT_SOURCE_REPO",
    "ODYSSEUS_AGENT_WORKTREE_ROOT",
    "ODYSSEUS_AGENT_BASE_BRANCH",
    "ODYSSEUS_AGENT_APPROVAL_TTL_SECONDS",
    "ODYSSEUS_AGENT_GITHUB_TOKEN",
    "ODYSSEUS_GITHUB_APP_ID",
    "ODYSSEUS_GITHUB_APP_INSTALLATION_ID",
    "ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _PUBLISH_ENV:
        monkeypatch.delenv(name, raising=False)


def test_publishing_is_disabled_without_the_flag():
    assert load_config().publish_enabled is False
    assert any("ODYSSEUS_AGENT_PUBLISH_ENABLED" in b for b in publish_blockers())


@pytest.mark.parametrize("value", ["0", "false", "off", "", "yes please", "TRUE ish"])
def test_only_explicit_affirmatives_enable_publishing(monkeypatch, value):
    monkeypatch.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", value)
    assert load_config().publish_enabled is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_affirmative_values_enable_the_flag(monkeypatch, value):
    monkeypatch.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", value)
    assert load_config().publish_enabled is True


def test_flag_alone_is_not_enough_to_publish(monkeypatch, tmp_path):
    monkeypatch.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", "1")
    monkeypatch.setenv("ODYSSEUS_AGENT_SOURCE_REPO", str(tmp_path))
    blockers = publish_blockers()
    assert any("ODYSSEUS_AGENT_REPO" in b for b in blockers)
    assert any("credential" in b for b in blockers)


def test_fully_configured_deployment_has_no_blockers(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("ODYSSEUS_AGENT_PUBLISH_ENABLED", "1")
    monkeypatch.setenv("ODYSSEUS_AGENT_REPO", "acme/widgets")
    monkeypatch.setenv("ODYSSEUS_AGENT_SOURCE_REPO", str(tmp_path))
    monkeypatch.setenv("ODYSSEUS_AGENT_GITHUB_TOKEN", "x" * 40)
    assert publish_blockers() == []


@pytest.mark.parametrize(
    "slug",
    ["", "acme", "acme/widgets/extra", "../etc", "acme/../widgets",
     "-acme/widgets", "acme/widgets.git", "acme/wid gets"],
)
def test_bad_repo_slugs_are_rejected(monkeypatch, slug):
    monkeypatch.setenv("ODYSSEUS_AGENT_REPO", slug)
    assert load_config().repo_slug == ""
    assert not is_valid_repo_slug(slug)


def test_remote_url_is_derived_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("ODYSSEUS_AGENT_REPO", "acme/widgets")
    monkeypatch.setenv("ODYSSEUS_AGENT_SOURCE_REPO", str(tmp_path))
    assert load_config().remote_url == "https://github.com/acme/widgets.git"


@pytest.mark.parametrize("raw,expected", [("30", 60), ("999999", 3600), ("nope", 900), ("", 900)])
def test_approval_ttl_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("ODYSSEUS_AGENT_APPROVAL_TTL_SECONDS", raw)
    assert load_config().approval_ttl_s == expected


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "dev",
        "agent/odysseus",
        "agent/odysseus/",
        "agent/odysseus/../../main",
        "agent/odysseus/..",
        "--upload-pack=touch /tmp/x",
        "agent/odysseus/-x",
        "agent/odysseus/a b",
        "agent/odysseus/x@{1}",
        "agent/odysseus/x.lock",
        "agent/odysseus/x\nmain",
        "refs/heads/main",
        "agent/odysseus/" + "x" * 300,
    ],
)
def test_branches_outside_the_agent_namespace_are_rejected(branch):
    assert validate_agent_branch(branch, BRANCH_PREFIX) == ""


def test_valid_agent_branches_survive():
    assert validate_agent_branch("agent/odysseus/cache-fix", BRANCH_PREFIX) == "agent/odysseus/cache-fix"
    assert branch_leaf_from_name("cache-fix", BRANCH_PREFIX) == "agent/odysseus/cache-fix"
    assert branch_leaf_from_name("agent/odysseus/cache-fix", BRANCH_PREFIX) == "agent/odysseus/cache-fix"


def test_branch_name_cannot_smuggle_a_prefix_then_escape():
    # A value that *starts* with the namespace but walks out of it must not
    # satisfy the prefix check.
    assert branch_leaf_from_name("agent/odysseus/../../main", BRANCH_PREFIX) == ""


def test_normalize_branch_rejects_control_characters():
    assert normalize_branch("agent/odysseus/x\x00y") == ""
    assert normalize_branch("agent/odysseus/x\ty") == ""


def test_sha_must_be_a_full_lowercase_hex_id():
    assert is_valid_sha("a" * 40)
    assert is_valid_sha("b" * 64)
    assert not is_valid_sha("A" * 40)      # uppercase
    assert not is_valid_sha("abc123")      # abbreviated
    assert not is_valid_sha("z" * 40)
    assert not is_valid_sha(None)


def test_worktree_path_stays_under_the_root(tmp_path):
    root = str(tmp_path / "wt")
    os.makedirs(root, exist_ok=True)
    path = safe_worktree_path(root, "agent/odysseus/deep/name", BRANCH_PREFIX)
    assert path is not None
    assert os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)
    # The nested branch is flattened rather than creating a directory tree.
    assert os.path.basename(path) == "deep__name"


def test_worktree_path_refuses_a_branch_that_is_not_ours(tmp_path):
    assert safe_worktree_path(str(tmp_path), "main", BRANCH_PREFIX) is None


@pytest.mark.parametrize(
    "path,category",
    [
        (".github/workflows/ci.yml", sensitive.WORKFLOWS),
        (".github/actions/setup/action.yml", sensitive.WORKFLOWS),
        ("Dockerfile", sensitive.DOCKER),
        ("docker/entrypoint.sh", sensitive.DOCKER),
        ("docker-compose.gpu-amd.yml", sensitive.DOCKER),
        ("odysseus-ui.service", sensitive.DEPLOYMENT),
        ("requirements.txt", sensitive.DEPLOYMENT),
        ("install-service.sh", sensitive.DEPLOYMENT),
        ("core/auth.py", sensitive.AUTH),
        ("routes/auth_routes.py", sensitive.AUTH),
        (".env.example", sensitive.SECRETS),
        ("certs/server.pem", sensitive.SECRETS),
        ("src/secret_storage.py", sensitive.SECRETS),
        ("mcp_servers/email_server.py", sensitive.MCP_PERMISSIONS),
        ("src/tool_security.py", sensitive.MCP_PERMISSIONS),
        (".claude/settings.json", sensitive.MCP_PERMISSIONS),
    ],
)
def test_sensitive_paths_are_classified(path, category):
    assert category in sensitive.categories_for(path)


@pytest.mark.parametrize(
    "path", ["src/chat_handler.py", "docs/setup.md", "static/js/app.js", "tests/test_x.py"],
)
def test_ordinary_paths_are_not_sensitive(path):
    assert sensitive.categories_for(path) == []


def test_leading_dot_slash_does_not_declassify_a_workflow():
    assert sensitive.categories_for("./.github/workflows/ci.yml") == [sensitive.WORKFLOWS]


def test_classification_digest_changes_with_the_file_set():
    one = sensitive.classify([".github/workflows/ci.yml"])
    two = sensitive.classify([".github/workflows/ci.yml", "Dockerfile"])
    assert sensitive.digest(one) != sensitive.digest(two)
    # Same set in a different order is the same digest.
    assert sensitive.digest(sensitive.classify(["Dockerfile", ".github/workflows/ci.yml"])) == \
        sensitive.digest(two)


def test_digest_of_an_empty_classification_is_stable():
    assert sensitive.digest({}) == sensitive.digest(sensitive.classify(["README.md"]))


def test_file_tools_refuse_the_approval_state_directory(monkeypatch, tmp_path):
    """The approval records are the trust anchor for human-gated publishing.

    They live under the data dir, which is an allowed root for the agent's file
    tools, so without an explicit deny the agent could write itself a granted
    record and publish with no human involved.
    """
    from src.tool_execution import _resolve_tool_path

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ODYSSEUS_AGENT_STATE_DIR", str(state))

    # A sibling under the same temp root resolves, so the refusals below are
    # the state-dir deny rule and not merely "outside the allowlist".
    sibling = tmp_path / "ordinary.txt"
    sibling.write_text("fine")
    assert _resolve_tool_path(str(sibling))

    for candidate in (
        str(state),
        str(state / "requests" / "abc.json"),
        str(state / ".approval_key"),
        str(state / "locks" / "approvals.lock"),
    ):
        with pytest.raises(ValueError, match="not allowed|sensitive|denied|outside"):
            _resolve_tool_path(candidate)

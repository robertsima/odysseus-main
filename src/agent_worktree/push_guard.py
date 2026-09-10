"""Catch git commands that can never succeed from the shell tool.

The publishing credential lives only inside the ``manage_agent_worktree`` flow.
A ``git push`` typed into ``bash`` has no token, no credential helper and no
terminal to prompt at, so it dies with a bare exit 128 and a message about
authentication. The model reads that as "the token is wrong", goes looking for a
GitHub integration that does not exist, and burns rounds getting nowhere. That
exact loop is what this module exists to break.

Detection is deliberately narrow in two directions:

* Only commands that contact a remote are matched. Local work (``status``,
  ``diff``, ``add``, ``commit``, ``branch``, ``switch``) must keep working
  normally, because that is how the agent is supposed to prepare a change.
* Only the **Odysseus** checkout is redirected to ``manage_agent_worktree``.
  That tool publishes one repository — the configured ``ODYSSEUS_AGENT_REPO`` —
  so pointing a push in some other project at it is worse than useless. In the
  2026-09-10 logs a push in ``/app/data/development/dog-trainer`` was redirected
  there, and the agent dutifully created an Odysseus worktree branch called
  ``agent/odysseus/umni-publish``, removed it, hunted for a GitHub integration,
  and reported the work done without publishing anything. A third-party
  repository gets guidance about *itself*, and is left alone entirely when it
  carries a credential of its own and the push can genuinely succeed.
"""

from __future__ import annotations

import os
import re
from typing import Optional, Tuple

# `git [-C path] [--git-dir=...] ... push`, anywhere in a compound command.
# Options between `git` and the subcommand are skipped, so `git -C /repo push`
# matches while `git log --grep=push` does not: `--grep=push` is consumed as an
# option, and `log` has already claimed the subcommand slot.
# The leading group anchors `git` to a command position (start of line, or after
# a shell separator) rather than to any whitespace. Without that, `echo git push`
# would be blocked as if it were a real push.
_COMMAND_START = r"(?:^|[;&|(\n])\s*"

_GIT_PUSH = re.compile(
    _COMMAND_START + r"git\s+(?:-[^\s]+(?:\s+[^\s-][^\s]*)?\s+)*push\b",
    re.IGNORECASE,
)

# `gh pr create` and friends, which would need the same missing credential.
_GH_PUBLISH = re.compile(
    _COMMAND_START + r"gh\s+(?:pr\s+create|repo\s+create|release\s+create)\b",
    re.IGNORECASE,
)

# Where the command would run: `git -C <path>`, `git --git-dir=<path>`, or a
# `cd <path>` earlier in the same compound command.
_GIT_C = re.compile(r"\bgit\s+(?:-[^\s]+\s+)*-C\s+(?P<path>\"[^\"]+\"|'[^']+'|\S+)", re.IGNORECASE)
_GIT_DIR = re.compile(r"--git-dir[= ](?P<path>\"[^\"]+\"|'[^']+'|\S+)", re.IGNORECASE)
_CD = re.compile(_COMMAND_START + r"cd\s+(?P<path>\"[^\"]+\"|'[^']+'|\S+)", re.IGNORECASE)

ESCAPE_HATCH_ENV = "ODYSSEUS_AGENT_ALLOW_BASH_PUSH"


def _guard_disabled() -> bool:
    """Operators with their own credential setup can turn this off."""
    return (os.getenv(ESCAPE_HATCH_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def detect_publish_command(command: object) -> Optional[str]:
    """Return "git-push" / "gh" for a command that would contact a remote."""
    if not isinstance(command, str) or not command.strip():
        return None
    if _GIT_PUSH.search(command):
        return "git-push"
    if _GH_PUBLISH.search(command):
        return "gh"
    return None


def _unquote(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def target_repository(command: object, cwd: Optional[str] = None) -> str:
    """Best-effort filesystem path the publish command would act on.

    Falls back to ``cwd`` (the agent's working directory) when the command
    names no path of its own.
    """
    text = command if isinstance(command, str) else ""
    for pattern, is_git_dir in ((_GIT_C, False), (_GIT_DIR, True)):
        match = pattern.search(text)
        if match:
            path = _unquote(match.group("path"))
            if is_git_dir and os.path.basename(path.rstrip("/")) == ".git":
                path = os.path.dirname(path.rstrip("/"))
            if path:
                return os.path.realpath(path)
    match = _CD.search(text)
    if match:
        path = _unquote(match.group("path"))
        if path and not path.startswith("-"):
            return os.path.realpath(path)
    return os.path.realpath(cwd or os.getcwd())


def _same_path(a: str, b: str) -> bool:
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


def _is_inside(path: str, root: str) -> bool:
    if not root:
        return False
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except (OSError, ValueError):
        return False


def is_odysseus_repository(path: str, cfg=None) -> bool:
    """True when `path` is the Odysseus checkout manage_agent_worktree owns,
    or one of the worktrees it creates from that checkout."""
    try:
        from src.agent_worktree.config import load_config

        cfg = cfg or load_config()
    except Exception:
        return False
    return bool(
        _same_path(path, cfg.source_repo)
        or _is_inside(path, cfg.source_repo)
        or _is_inside(path, cfg.worktree_root)
    )


def _git_config_text(repo: str) -> str:
    """Contents of the repository's own git config, or "" when unreadable."""
    try:
        git_path = os.path.join(repo, ".git")
        if os.path.isfile(git_path):
            pointer = open(git_path, encoding="utf-8", errors="replace").read().strip()
            if pointer.startswith("gitdir:"):
                git_path = pointer.split(":", 1)[1].strip()
                if not os.path.isabs(git_path):
                    git_path = os.path.join(repo, git_path)
            else:
                return ""
        with open(os.path.join(git_path, "config"), encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


_URL_WITH_USERINFO = re.compile(r"url\s*=\s*\w+://[^/\s]+@", re.IGNORECASE)
_CREDENTIAL_SECTION = re.compile(r"^\s*\[credential", re.IGNORECASE | re.MULTILINE)
_SSH_REMOTE = re.compile(r"url\s*=\s*(?:ssh://|git@)", re.IGNORECASE)


def repository_has_own_credential(repo: str) -> bool:
    """True when a push from this repository could plausibly authenticate.

    Blocking a push that would actually have worked is the worse failure — it
    reads to the user as "git integration broke". So a repository that carries
    a credential helper, an SSH remote, or a remote URL with userinfo is left
    alone, as is any repository when a global ``~/.git-credentials`` exists.
    """
    config = _git_config_text(repo)
    if config and (
        _CREDENTIAL_SECTION.search(config)
        or _URL_WITH_USERINFO.search(config)
        or _SSH_REMOTE.search(config)
    ):
        return True
    home = os.environ.get("HOME") or os.path.expanduser("~")
    try:
        store = os.path.join(home, ".git-credentials")
        return os.path.isfile(store) and os.path.getsize(store) > 0
    except OSError:
        return False


def _remote_url(repo: str) -> str:
    match = re.search(r"url\s*=\s*(\S+)", _git_config_text(repo) or "")
    if not match:
        return ""
    # Never echo embedded credentials back into the transcript.
    return re.sub(r"://[^/\s]*@", "://", match.group(1))


def guidance(kind: str) -> str:
    """The message the agent gets instead of a confusing authentication error."""
    from src.agent_worktree.config import BRANCH_PREFIX, load_config, publish_blockers

    try:
        cfg = load_config()
        blockers = publish_blockers(cfg)
    except Exception:
        cfg, blockers = None, ["configuration could not be read"]

    what = "Pushing to a remote" if kind == "git-push" else "Creating a pull request or release"
    lines = [
        f"{what} from bash is not possible: the shell tool holds no git credential, "
        "so this command can only fail with an authentication error.",
        "",
        "Use the manage_agent_worktree tool instead. It is the only path that has a "
        "credential, and it opens a draft pull request after a human approves:",
        "",
        '  1. manage_agent_worktree {"action": "start", "name": "<short-task-name>"}',
        "     Work inside the worktree path it returns, not in any other checkout.",
        '  2. manage_agent_worktree {"action": "commit", "message": "..."}',
        '  3. manage_agent_worktree {"action": "request_publish", "title": "...", "body": "..."}',
        "  4. Show the operator the changed-file list and the approval command it "
        "returns, then wait for them to give you an approval code.",
        '  5. manage_agent_worktree {"action": "publish", "request_id": "...", '
        '"approval_code": "<code from the operator>"}',
        "",
        f"Branches live under {BRANCH_PREFIX}. Committing locally in bash is fine; "
        "publishing is not.",
    ]
    if blockers:
        lines += [
            "",
            "Publishing is currently unavailable for these reasons, which only the "
            "operator can fix:",
        ]
        lines += [f"  - {reason}" for reason in blockers]
        lines += [
            "",
            "Tell the operator to run `scripts/odysseus-agent-worktree doctor` inside "
            "the container. Do not retry the push and do not go looking for a GitHub "
            "integration or an API token; neither exists here.",
        ]
    return "\n".join(lines)


def foreign_repository_guidance(kind: str, repo: str) -> str:
    """Guidance for a publish command in a repository that is NOT Odysseus.

    ``manage_agent_worktree`` publishes exactly one repository. Naming it here
    sends the agent to create an Odysseus branch for someone else's project,
    which is what happened on 2026-09-10, so this message deliberately tells it
    not to.
    """
    try:
        from src.agent_worktree.config import load_config

        slug = load_config().repo_slug or "the Odysseus repository"
    except Exception:
        slug = "the Odysseus repository"
    remote = _remote_url(repo)
    what = "Pushing to a remote" if kind == "git-push" else "Creating a pull request or release"
    lines = [
        f"{what} from bash failed before it ran: no credential is configured for "
        f"{repo}" + (f" (remote {remote})" if remote else "") + ", and the shell tool "
        "carries none of its own.",
        "",
        f"This is NOT the Odysseus checkout, so manage_agent_worktree cannot publish it — "
        f"that tool only ever pushes {slug}. Do not start an Odysseus worktree for this "
        "work; it would create a branch in the wrong repository.",
        "",
        "What you can still do here:",
        "  - Everything local: status, diff, add, commit, branch, tag. Commit the work "
        "so it is ready to push.",
        "  - Report to the user that the commit is ready and the push needs a credential.",
        "",
        "What the operator has to do once, outside this chat:",
        "  - push from the host, or",
        "  - give this repository a credential (a credential helper, an SSH remote, or a "
        "token in its remote URL). Any of those and this command will be allowed through "
        "on the next attempt.",
        "",
        "Do not retry the push and do not look for a GitHub integration or API token in "
        "Odysseus; the one that exists publishes only " + slug + ".",
    ]
    return "\n".join(lines)


def _names_agent_branch(command: object) -> bool:
    """A push of an ``agent/odysseus/*`` branch is Odysseus work wherever it
    is typed — the branch prefix is the tool's own namespace."""
    try:
        from src.agent_worktree.config import BRANCH_PREFIX
    except Exception:
        BRANCH_PREFIX = "agent/odysseus/"
    return BRANCH_PREFIX in (command if isinstance(command, str) else "")


def check(command: object, cwd: Optional[str] = None) -> Optional[dict]:
    """Tool-result dict to return instead of running `command`, or None.

    ``cwd`` is the directory the shell tool would run in; it decides which
    repository a bare ``git push`` targets.
    """
    if _guard_disabled():
        return None
    kind = detect_publish_command(command)
    if not kind:
        return None
    repo = target_repository(command, cwd)
    if is_odysseus_repository(repo) or _names_agent_branch(command):
        return {
            "error": guidance(kind),
            "exit_code": 1,
            "blocked_reason": "publish_requires_agent_worktree_tool",
        }
    if repository_has_own_credential(repo):
        # It can actually authenticate — blocking it is the regression.
        return None
    return {
        "error": foreign_repository_guidance(kind, repo),
        "exit_code": 1,
        "blocked_reason": "publish_needs_repository_credential",
        "repository": repo,
    }

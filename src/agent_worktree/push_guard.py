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

# An option's value may carry a quoted segment with spaces inside it:
# `git -c credential.helper='!f() { echo password=x; }; f' push` is the agent
# improvising a credential, and it has to be caught like any other push.
_OPTION_VALUE = r"(?:\"[^\"]*\"|'[^']*'|[^\s\"'-][^\s\"']*(?:\"[^\"]*\"|'[^']*')?\S*)"
_GIT_PUSH = re.compile(
    _COMMAND_START + r"git\s+(?:-\S+(?:\s+" + _OPTION_VALUE + r")?\s+)*push\b",
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
# `helper = store --file=/x`, `helper = cache`, `helper = !gh auth git-credential`
_CREDENTIAL_HELPER = re.compile(r"^\s*helper\s*=\s*(?P<helper>.*?)\s*$", re.IGNORECASE | re.MULTILINE)
_STORE_FILE = re.compile(r"--file(?:=|\s+)(?P<path>\"[^\"]+\"|'[^']+'|\S+)")


def _non_empty_file(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _agent_home() -> str:
    """The HOME git sees from the shell tool.

    tool_execution pins the agent subprocess's HOME to DATA_DIR, so that is
    where its `~/.gitconfig` and `~/.git-credentials` resolve — not the app
    process's own HOME, which is /root in the container and is emptied by
    every rebuild. Checking the wrong one would block a push a global helper
    on the data volume was about to authenticate.
    """
    try:
        from src import constants

        home = constants.DATA_DIR
    except Exception:
        home = ""
    return home or os.environ.get("HOME") or os.path.expanduser("~")


def _global_credential_stores() -> list:
    """Where `credential.helper = store` reads from when no --file is given."""
    home = _agent_home()
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return [os.path.join(home, ".git-credentials"), os.path.join(xdg, "git", "credentials")]


def _global_config_texts() -> list:
    """The global git configs the shell tool's git reads, in git's order."""
    home = _agent_home()
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    paths = [
        os.environ.get("GIT_CONFIG_GLOBAL") or os.path.join(home, ".gitconfig"),
        os.path.join(xdg, "git", "config"),
    ]
    texts = []
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                texts.append(handle.read())
        except OSError:
            continue
    return texts


def _credential_helpers(repo: str) -> list:
    """`credential.helper` values from the repository config and the global
    configs, as git would see them."""
    helpers = []
    for config in [_git_config_text(repo)] + _global_config_texts():
        if config and _CREDENTIAL_SECTION.search(config):
            helpers += [m.group("helper").strip().strip("\"'") for m in _CREDENTIAL_HELPER.finditer(config)]
    return helpers


def _store_paths(helper: str) -> list:
    match = _STORE_FILE.search(helper)
    if match:
        return [os.path.expanduser(_unquote(match.group("path")))]
    return _global_credential_stores()


def _helper_can_authenticate(helper: str) -> bool:
    """Whether a `credential.helper` value has a credential to hand git.

    `store` is only as good as the file behind it: a container rebuilt from
    the image keeps the repository (it lives on the data volume) but starts
    with an empty home, so the config still says `store` while the store is
    gone. `cache` is an in-memory daemon that dies with the process and has no
    terminal here to be refilled from. Any other helper (a keychain, a custom
    `!` command) is trusted, because there is no way to check it from outside.
    """
    helper = (helper or "").strip().strip("\"'")
    if not helper:
        return False
    name = helper.split(None, 1)[0]
    if name == "cache":
        return False
    if name == "store":
        return any(_non_empty_file(p) for p in _store_paths(helper))
    return True


def empty_credential_store(repo: str) -> Optional[str]:
    """The file a `store` helper would read when the repository names one
    that is missing or empty, so the guidance can say what actually broke."""
    for helper in _credential_helpers(repo):
        if helper.split(None, 1)[:1] == ["store"] and not _helper_can_authenticate(helper):
            return _store_paths(helper)[0]
    return None


def repository_has_own_credential(repo: str) -> bool:
    """True when a push from this repository could plausibly authenticate.

    Blocking a push that would actually have worked is the worse failure — it
    reads to the user as "git integration broke". So a repository with an SSH
    remote, a remote URL with userinfo, or a credential helper (in its own
    config or the agent's global one) that has something to hand over is left
    alone, as is any repository when a global ``~/.git-credentials`` exists.

    A ``[credential]`` section on its own is not enough. ``helper = store``
    with an empty store is the 2026-09-11 failure: the guard waved the push
    through as credentialed, git exited 128, and the agent spent seven rounds
    probing helpers and SSH keys before reporting "blocked by authentication".
    """
    config = _git_config_text(repo)
    if config and (_URL_WITH_USERINFO.search(config) or _SSH_REMOTE.search(config)):
        return True
    if any(_helper_can_authenticate(helper) for helper in _credential_helpers(repo)):
        return True
    return any(_non_empty_file(p) for p in _global_credential_stores())


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
    store = empty_credential_store(repo)
    what = "Pushing to a remote" if kind == "git-push" else "Creating a pull request or release"
    where = f"{repo}" + (f" (remote {remote})" if remote else "")
    if store:
        first = (
            f"{what} from bash failed before it ran: {where} names `credential.helper = store`, "
            f"but {store} is missing or empty, and the shell tool carries no credential of its "
            "own. A container rebuilt from the image keeps the checkout (it lives on the data "
            "volume) but starts with an empty home, which is how a store goes missing."
        )
    else:
        first = (
            f"{what} from bash failed before it ran: no credential is configured for {where}, "
            "and the shell tool carries none of its own."
        )
    lines = [
        first,
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
    ]
    if store:
        lines += [
            f"  - refill {store} (`git credential approve`, or copy it back), and keep it under "
            "/app/data with `helper = store --file=...` so the next rebuild does not wipe it, or",
        ]
    lines += [
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

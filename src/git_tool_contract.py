"""Dependency-free action contracts shared by Git dispatch and approvals.

Native models sometimes fill every property in a multi-action schema. Only
known unused/defaultable neutral placeholders are interchangeable with omission.
Required fields, meaningful overrides and unknown keys retain their exact meaning.
"""

import re

LOCAL_ACTIONS = {
    "status": set(),
    "log": {"limit", "ref"},
    "diff": {"staged"},
    "branches": set(),
    "remotes": set(),
    "stage": {"paths"},
    "unstage": {"paths"},
    "commit": {"message", "author_name", "author_email"},
    "branch": {"name", "ref"},
    "tag": {"name", "ref"},
}
CREATION_ACTIONS = {
    "clone": {"source", "branch", "depth"},
    "init": {"initial_branch"},
}
HISTORY_ACTIONS = {
    "stash_list": set(),
    "stash_create": {"message"},
    "stash_apply": {"index", "expected_target"},
    "stash_pop": {"index", "expected_target"},
    "stash_drop": {"index", "expected_target"},
    "reset": {"ref", "expected_head", "expected_target"},
    "rebase": {"ref", "expected_head", "expected_target"},
}
REMOTE_ACTIONS = {
    "fetch": set(),
    "fetch_branch": {"remote", "remote_branch"},
    "pull": set(),
    "pull_with_restore": set(),
    "switch": {"name"},
    "set_upstream": {"remote", "remote_branch"},
    "push": {"remote_branch", "expected_head"},
    "force_push_with_lease": {"remote_branch", "expected_head", "expected_target"},
    "delete_remote_branch": {"remote_branch", "expected_head", "expected_target"},
    "merge": {"ref", "expected_head", "expected_target"},
    "delete_branch": {"name", "expected_head", "expected_target"},
}
RISKY_ACTIONS = frozenset(
    {
        "push",
        "force_push_with_lease",
        "delete_remote_branch",
        "merge",
        "delete_branch",
        "stash_pop",
        "stash_drop",
        "reset",
        "rebase",
    }
)
REQUIRED_REVISIONS = {
    "push": {"expected_head"},
    "force_push_with_lease": {"expected_head", "expected_target"},
    "delete_remote_branch": {"expected_head", "expected_target"},
    "merge": {"expected_head", "expected_target"},
    "delete_branch": {"expected_head", "expected_target"},
    "stash_pop": {"expected_target"},
    "stash_drop": {"expected_target"},
    "reset": {"expected_head", "expected_target"},
    "rebase": {"expected_head", "expected_target"},
}
# Operator policy: agents may inspect, commit, create branches and tags, and
# push fast-forward, but never delete or discard anything. These actions keep
# their contracts above (the service code and approval copy still exist) so
# old approvals and arguments parse the same way, but dispatch refuses them
# outright -- before validation and before any approval is requested. No
# approval, standing grant or approval mode re-enables them.
POLICY_FORBIDDEN_ACTIONS = {
    "delete_branch": "deleting a local branch",
    "delete_remote_branch": "deleting a remote branch",
    "force_push_with_lease": "force-pushing (rewriting a remote branch)",
    "stash_drop": "deleting a stash entry",
}
# Unknown actions are already unsupported; these words only make the refusal
# say "policy" instead of "unsupported" for the deletes/discards models tend to
# invent (force_push, clean, reflog_expire, worktree_remove, gc_prune, ...).
_FORBIDDEN_ACTION_WORDS = frozenset(
    {
        "delete", "del", "remove", "rm", "drop", "clear", "prune", "purge",
        "clean", "force", "expire", "destroy", "wipe", "gc", "mirror",
        "discard", "hard", "restore", "checkout",
    }
)


def policy_refusal(action):
    """The policy error text for a delete/discard action, or None."""
    name = str(action or "").strip().lower()
    reason = POLICY_FORBIDDEN_ACTIONS.get(name)
    known = any(
        name in group
        for group in (LOCAL_ACTIONS, CREATION_ACTIONS, HISTORY_ACTIONS, REMOTE_ACTIONS)
    )
    if reason is None and not known:
        words = set(re.split(r"[^a-z0-9]+", name))
        if words & _FORBIDDEN_ACTION_WORDS or any(w.startswith("filter") for w in words):
            reason = "deleting, force-updating or discarding repository data"
    if reason is None:
        return None
    return (
        f"Git action {name!r} is not permitted by policy: {reason} is refused. "
        "Agents may inspect, stage, commit, create branches/tags and push "
        "fast-forward to non-protected branches, but must not delete, force-push "
        "or discard work. Ask the user to do it themselves if it is really needed."
    )


# These fields have explicit service defaults; required targets/revision proofs
# never belong here. A zero log limit is a neutral sentinel, not unbounded output.
DEFAULTABLE_FIELDS = {
    "log": {"limit", "ref"},
    "diff": {"staged"},
    "commit": {"author_name", "author_email"},
    "branch": {"ref"},
    "tag": {"ref"},
    "push": {"remote_branch"},
    "force_push_with_lease": {"remote_branch"},
    "set_upstream": {"remote"},
    "fetch_branch": {"remote"},
    "clone": {"branch", "depth"},
    "init": {"initial_branch"},
    "stash_create": {"message"},
    "stash_apply": {"index"},
    "stash_pop": {"index"},
    "stash_drop": {"index"},
}
GIT_FIELDS = {"action", "repository"}.union(
    *LOCAL_ACTIONS.values(),
    *CREATION_ACTIONS.values(),
    *HISTORY_ACTIONS.values(),
    *REMOTE_ACTIONS.values(),
)
WORKTREE_FIELDS = {
    "action",
    "repository",
    "name",
    "branch",
    "message",
    "title",
    "body",
    "request_id",
    "approval_code",
}


def _neutral_placeholder(name, value):
    return (
        value is None
        or value == ""
        or (name == "paths" and isinstance(value, list) and not value)
        or (name == "staged" and value is False)
        or (name == "limit" and type(value) in (int, float) and value == 0)
        or (name == "depth" and type(value) in (int, float) and value == 0)
        or (name == "index" and type(value) in (int, float) and value == 0)
    )


def _prune_unused(args, accepted, declared):
    return {
        name: value
        for name, value in args.items()
        if name in accepted
        or name not in declared
        or not _neutral_placeholder(name, value)
    }


def normalize_git_arguments(args):
    if not isinstance(args, dict):
        return args
    action = str(args.get("action") or "repositories").strip().lower()
    fields = LOCAL_ACTIONS.get(
        action,
        CREATION_ACTIONS.get(
            action, HISTORY_ACTIONS.get(action, REMOTE_ACTIONS.get(action))
        ),
    )
    if action == "repositories":
        accepted = {"action"}
    elif fields is not None:
        accepted = {"action", "repository"} | fields
    else:
        return dict(args)
    return _prune_unused(
        args, accepted - DEFAULTABLE_FIELDS.get(action, set()), GIT_FIELDS
    )


def normalize_worktree_repo_arguments(args):
    action = str(args.get("action") or "").strip().lower()
    if action not in {"repo_list", "repo_status", "repo_pull"}:
        return dict(args)
    accepted = {"action"} if action == "repo_list" else {"action", "repository"}
    return _prune_unused(args, accepted, WORKTREE_FIELDS)

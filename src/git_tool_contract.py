"""Dependency-free action contracts shared by Git dispatch and approvals.

Native models sometimes fill every property in a multi-action schema. Only
known unused/defaultable neutral placeholders are interchangeable with omission.
Required fields, meaningful overrides and unknown keys retain their exact meaning.
"""

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
REMOTE_ACTIONS = {
    "fetch": set(),
    "pull": set(),
    "switch": {"name"},
    "set_upstream": {"remote", "remote_branch"},
    "push": {"remote_branch", "expected_head"},
    "merge": {"ref", "expected_head", "expected_target"},
    "delete_branch": {"name", "expected_head", "expected_target"},
}
RISKY_ACTIONS = frozenset({"push", "merge", "delete_branch"})
# These fields have explicit service defaults; required targets/revision proofs
# never belong here. A zero log limit is a neutral sentinel, not unbounded output.
DEFAULTABLE_FIELDS = {
    "log": {"limit", "ref"},
    "diff": {"staged"},
    "commit": {"author_name", "author_email"},
    "branch": {"ref"},
    "tag": {"ref"},
    "push": {"remote_branch"},
    "set_upstream": {"remote"},
}
GIT_FIELDS = {"action", "repository"}.union(
    *LOCAL_ACTIONS.values(), *REMOTE_ACTIONS.values()
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
    fields = LOCAL_ACTIONS.get(action, REMOTE_ACTIONS.get(action))
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

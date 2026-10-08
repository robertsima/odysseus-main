---
name: claude-code-delegation
description: Delegating bounded coding work in an approved Git checkout to the local Claude Code CLI through delegate_to_claude_code, including preflight, verification and parallel jobs.
metadata:
  version: 1.1.0
  category: dev
  status: published
  source: bundled
---

# Claude Code delegation

Claude Code is a coding agent that Agamemnon runs as a local subprocess, not a chat model: `chat_with_model` and `list_models` never list it, and `/app` is not a repository it can work in. Everything goes through `delegate_to_claude_code` (outside chat, `/api/claude-code/tasks`).

You decide scope, risk and acceptance. Claude Code implements inside one checkout. Agamemnon never pushes for it and never handles its Anthropic credentials: the operator signs the unmodified binary in, and the harness hands it only a scoped Agamemnon token for calling back into this instance when configured. Read [terms-and-boundaries.md](references/terms-and-boundaries.md) before proposing any change to how Claude is reached.

## Preflight (once per session, before the first delegation)

1. Call `delegate_to_claude_code` with `{"action": "status"}`.
   - `ready: true` means the binary exists, supports the headless flags and is signed in.
   - `repositories` lists every approved checkout or worktree with its branch. `default_repository` is used when a delegation names none.
   - `hints` names the exact repair for anything missing (binary path, sign-in, no checkout under the roots). Report it. Sign-in cannot be fixed from chat.
   - `update_required` is set when an earlier run found the CLI too old for its model. `{"action": "update"}` (admin) runs the binary's own updater and reports `version_before` and `version_after`; it is refused while any delegation runs.
2. When the user names a repository, match it against `repositories`. If it is not listed, say so and offer the listed ones. The path always comes from `repositories`, never from a guess and never `/app`.
3. For Agamemnon itself, use the dedicated agent worktree (an `agent_worktrees/...` entry) when one exists, so a delegation cannot disturb the running app's files.

## Delegation loop

1. Decide the change, risks and acceptance criteria first.
2. Build one compact prompt (a few thousand tokens at most) with:
   - one objective;
   - likely files, symbols or failing tests, as paths and names, never whole files or chat history;
   - constraints ("do not touch X", "keep the public API", "preserve unrelated changes and report overlaps");
   - the exact verification command (`pytest tests/test_x.py -q`);
   - "inspect before editing, commit with a descriptive message, report the files you changed and the test evidence".

   Credentials, tokens, private keys and personal data stay out of it.
3. Choose the mode:
   - `action: "run"` waits up to `timeout_seconds` (default 900). Use it for one change you will act on immediately. Set the timeout to what the task needs, because the process is killed at the limit and the tree is left as-is.
   - `action: "start"` returns a `task_id`. Use it for long jobs, for work you can do meanwhile, or for several repositories in parallel (one job per repository; jobs on one checkout queue, and `claude_code_max_concurrent_tasks` in Settings > Agent Tools > Claude Code caps the total). Then `{"action": "poll", "task_id": ...}`, `{"action": "list"}` and `{"action": "cancel", "task_id": ...}`.
4. Read the result, not just the exit code:
   - `result` is Claude's own report.
   - `branch`, `commit`, `changed_files` and `clean` come from git after the run and are the truth about what changed.
   - `permission_denials` lists actions Claude wanted but was refused (push, arbitrary shell, files outside the checkout). Mention them. If the task needs a test runner, retry once with `allowed_tools` including `Bash(pytest:*)`.
   - `is_error` or `exit_code != 0` with an empty `result` means the CLI failed before working. `error` carries stderr; rerun `status`. Known failures and their fixes are in [failures.md](references/failures.md).
5. Verify independently: inspect the diff with the workspace file tools and run the smallest relevant test.
6. Allow one narrow repair pass. After two unsuccessful passes, take the work over in the primary harness or escalate.
7. Publishing is a separate, human-gated step: `manage_agent_worktree` (`request_publish`, operator approval, `publish`). Claude Code cannot push, so never ask it to.
8. After acceptance, if the owner keeps a delegation log (an explicitly selected knowledge-base folder; use the owner's configured destination rather than guessing a personal vault name), record the outcome as [documentation-policy.md](references/documentation-policy.md) says. Skip the record when there is no log or the user calls the work throwaway.

## Acceptance

Accept only when all hold:
- the diff matches the requested scope and `changed_files` has no surprises;
- the stated verification ran and passed, or its failure is reported honestly;
- nothing depends on hidden session context.

Your summary to the user names the repository, branch, commit and files.

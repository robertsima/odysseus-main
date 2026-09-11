---
name: claude-code-delegation
description: "Delegate bounded coding work in an approved Git checkout to the locally installed Claude Code CLI through delegate_to_claude_code; preflight the integration, size the task, verify the result, and coordinate several Claude jobs from the primary harness."
version: 1.0.0
category: dev
tags: [delegation, claude-code, coding, multi-agent, worktree]
platforms: [linux, windows, macos]
requires_toolsets: [delegate_to_claude_code]
status: published
confidence: 0.9
source: bundled
created: "2026-09-10T22:15:45Z"
---

# Claude Code Delegation

Claude Code is a coding agent that Odysseus runs as a local subprocess. It is
**not** a chat model: `chat_with_model` and `list_models` will never find it,
and `/app` is not a repository it can work in. Everything goes through the
`delegate_to_claude_code` tool (or, outside chat, `/api/claude-code/tasks`).

The primary harness decides scope, risk, and acceptance. Claude Code is the
implementation worker inside one checkout. Odysseus never pushes on its
behalf and never handles its Anthropic credentials — the operator signs the
unmodified binary in with their own account, and the harness only hands it a
scoped Odysseus token so it can call back into this instance if that is
configured. Read [terms-and-boundaries.md](references/terms-and-boundaries.md)
before proposing any change to how Claude is reached.

1. Call `delegate_to_claude_code` with `{"action": "status"}`.
   - `ready: true` means the binary exists, supports the headless flags, and
     is signed in.
   - `repositories` lists every approved checkout/worktree with its branch;
     `default_repository` is what a delegation without `repository` uses.
   - `hints` names the exact repair when something is missing (binary path,
     sign-in, no checkout under the roots). Report it; do not try to fix a
     sign-in problem from the chat.
2. If the user names a repository, match it against `repositories`. If it is
   not listed, say so and offer the listed ones — never guess a path, and
   never pass the application root (`/app`).
3. For Odysseus itself, prefer the dedicated agent worktree (an
   `agent_worktrees/...` entry) over the source checkout when both exist, so a
   delegation cannot disturb the running app's files.

1. Decide the change, risks, and acceptance criteria first.
2. Build one compact prompt:
   - one objective;
   - likely files, symbols, or failing tests;
   - constraints ("do not touch X", "keep the public API");
   - the exact verification command (`pytest tests/test_x.py -q`);
   - "inspect before editing, commit with a descriptive message, report the
     files you changed and the test evidence".
3. Choose the mode:
   - `action: "run"` waits (up to `timeout_seconds`, default 900). Use it for
     a single change you will act on immediately.
   - `action: "start"` returns a `task_id` immediately. Use it when the job
     is long, when you have other work to do meanwhile, or when you want
     several repositories worked in parallel (one job per repository; jobs on
     the same checkout queue behind each other). Poll with
     `{"action": "poll", "task_id": ...}`; `{"action": "list"}` shows every
     job; `{"action": "cancel", "task_id": ...}` stops one.
4. Read the result, not just the exit code:
   - `result` is Claude's own report;
   - `branch`, `commit`, `changed_files`, `clean` are read from git after
     the run — treat them as the truth about what changed;
   - `permission_denials` lists actions Claude wanted but was not allowed
     (push, arbitrary shell, files outside the checkout). Mention them; if
     the task genuinely needs a test runner, pass `allowed_tools` including
     `Bash(pytest:*)` and retry once.
   - `is_error` or `exit_code != 0` with an empty `result`: the CLI failed
     before working (flag rejected, not signed in). `error` carries stderr;
     rerun `status`.
5. Verify independently: inspect the diff with the workspace file tools and
   run the smallest relevant test when the tools allow it.
6. One narrow repair pass at most. After two unsuccessful passes, stop
   delegating and handle or escalate the work in the primary harness.
7. Publishing is a separate, human-gated step: use `manage_agent_worktree`
   (`request_publish` → operator approval → `publish`). Claude Code cannot
   push and must not be asked to.
8. After acceptance, record the outcome in the `AI Mind` workspace so later
   sessions can orient without replaying this one. Follow
   [documentation-policy.md](references/documentation-policy.md): find the
   note with `search_documents` ("Claude Code Delegation"), append a dated
   entry with `edit_file` (create `AI Mind/Claude Code Delegation.md` with
   `write_file` only if the search finds nothing), keep it to project, task,
   outcome, files changed, checks reviewed, limitations. Skip the record when
   the user says the work is throwaway.

- Send paths, symbols, errors, and acceptance criteria — not whole files or
  the chat history. Keep prompts under a few thousand tokens.
- Never include credentials, tokens, private keys, or personal data.
- Treat the checkout as possibly dirty: ask Claude to preserve unrelated
  changes and to report overlaps instead of overwriting them.
- Do not raise `timeout_seconds` above what the task needs; the process is
  killed at the limit and the working tree is left as-is.
- Runs on one checkout are serialized; the aggregate limit is
  `claude_code_max_concurrent_tasks` (Settings > Tools > Claude Code).

| Symptom | Meaning | Do |
|---|---|---|
| "not an existing Git repository or worktree" | wrong path (e.g. `/app`) | use a path from `status`/`list_repositories` |
| "outside Claude Code approved roots" | path not under the configured roots | pick an approved one or ask the operator to add the root in Settings |
| `ready: false`, `auth.logged_in: false` | binary not signed in | tell the operator to sign in once as the container user |
| "binary unavailable" | wrong `claude_code_binary` | operator fixes the path in Settings > Tools > Claude Code |
| `permission_denials` mentions push/remote | Claude tried to publish | expected; publishing goes through `manage_agent_worktree` |
| exit 124 | timed out | narrow the task or split it |

- the diff matches the requested scope and `changed_files` has no surprises;
- the stated verification ran and passed, or the failure is reported honestly;
- nothing depends on hidden session context;
- your summary to the user names the repository, branch, commit, and files.

# Harness review — 2026-10-01: why agents deliver too little, too slowly

The user's report: results "too small", the agents "too bounded", and slow.
Evidence is a four-hour diagnostics bundle with messages (2026-10-01 13:00–17:00,
build 9fe57f7c) covering the "Agamemnon" theme work: one orchestrator chat
("Odysseus AI Admin Agent", gpt-6-luna on the ChatGPT/Codex route) and the nine
workers it started (Lead Engineer, Penpot Product Designer). The mechanisms that
changed are in [`agent-runtime.md`](agent-runtime.md); this page keeps the
evidence and what is left.

## What happened

The user asked for a theme that is "more than a color scheme… the swap [should]
change the layout", then "implement the rest of it using the team and spec driven
development with a review", then "make the icon more similar to the mockup we
made, i added it here". PR #38 delivered mostly a color scheme. PR #39 shipped a
headphones-and-microphone glyph as the "helmet" logo, and its contract test passed
it. Another round of workers followed before PR #40.

## What the logs showed

| Measure | Value |
|---|---|
| Agent rounds | 715 (orchestrator 475, nine workers ~240) |
| Rounds with exactly one tool call | 695 (97%) |
| Model time per round | p50 6.1 s; 7.2 s at 200k+ prompt tokens |
| Orchestrator prompt per round | ~187k tokens on average; one turn ran 200+ rounds |
| Orchestrator lifetime input | 291M tokens since 2026-09-21 (225 messages, one chat) |
| Worker hand-backs naming an environment blocker | 7 of 9 |

**The orchestrator did the work itself.** While workers ran, it re-read and
grepped the files they were reading (one call per round at ~190k tokens), and it
edited the worktree they were editing (`apply_patch` 14:55 and 15:04). Workers
found files "claimed updated but absent" and briefs saying "parent has made
uncommitted changes".

**The scope shrank at every hop.** The loadouts say "technical lead for bounded
software delivery", "choose the smallest safe workflow", "allow one targeted
repair", "do not over-research, over-validate". The orchestrator's briefs added
limits the user never set: "targeted repair and verification only", "make only
necessary safe fix", "No heavy layout redesign". The user's own words never
reached a worker. Five of the nine workers were read-only reviewers or
inspectors, each handed one sliver.

**The environment blocked the workers:**

- Sandboxed `git` failed in every linked worktree (exit 128, `not a git
  repository: …/.git/worktrees/<slug>`): the worktree's `.git` points into the
  main checkout, which the sandbox did not mount.
- `manage_git diff` failed every time with "diff input must be at most 64 MiB".
  The cap was charged with the size of every tracked file, changed or not, so
  any repository with large assets could never diff. The 2026-09-30 sweep logged
  this as a large-diff problem; it was the whole-repository count.
- Workers could not read the user's uploaded `.penpot` file ("outside the
  workspace and outside the personal documents directory"). One worker called
  `ask_user` and ended.
- `pytest` in a resumed worker ran outside the worktree (it was not bound).

**Nobody could see the result.** Tool images (Penpot `render_preview`, browser
screenshots) were forwarded to the UI only, never to the model. The designer was
told "never claim visual verification without viewing a render" and could not
view one. No engineering agent could render the page or SVG it changed.

**The model lost its own thinking every round.** The upstream re-sync
(c72cb40c, 2026-09-18) dropped the loop's handling of encrypted reasoning items
that 8cca5a1e had added. llm_core kept requesting `reasoning.encrypted_content`
and the loop discarded it, so gpt-6-luna re-planned from the transcript after
every tool result. Its message `phase` (commentary vs final answer) was not
replayed either; `[responses-phase]` lines showed both phases.

**No parallel tool calls.** The Codex request never sent `parallel_tool_calls`,
and no rule asked for independent calls to be batched.

**Smaller:** one worker's hand-back was appended to the parent twice, a second
apart.

## What changed

| Problem | Change |
|---|---|
| git in linked worktrees | `shell_sandbox.linked_git_binds`: binds the repository's shared git dir (back-link verified), with `hooks`/`config` overlaid read-only; worktrees under the managed root are bound even when the repo-roots helper is empty; relative `gitdir` pointers resolved |
| `manage_git diff` 64 MiB | `repository_local._diff` charges caps only to changed files, skips stat-clean files without hashing (git's racy-clean rule), and shows a placeholder for an oversized changed file instead of failing |
| Attachments unreadable | `src/attachment_access.py`: a chat and its workers (same owner, any depth) may read the exact files attached in that lineage, read-only; bash/python get them as read-only sandbox binds |
| One call per round | `parallel_tool_calls: true` on the Codex route (dropped per host if refused); a base rule to batch independent calls |
| Reasoning dropped | Encrypted reasoning items re-ported onto the current loop, pruned in batches so the cached prefix breaks about once per four rounds |
| Phase not replayed | Assistant messages carry `responses_phase` and are replayed with it on the Codex route (disabled per host if refused) |
| Scope narrowing | `_DELEGATION_RULES` rewritten: the brief's scope is the whole request, one worker per user-visible outcome, done-when is what the person would check (rendered for visual work), don't redo or edit a running worker's work, resume the same worker |
| User's words lost | `manage_agent_loadout start` appends the root chat's latest request verbatim (clipped at 2,000 chars, with attachment names) |
| Two writers | `src/worktree_writers.py`: while a worker runs in a workspace, other sessions' `write_file`/`edit_file`/`apply_patch` there are refused with what to do instead (fails open) |
| Duplicate hand-back | `_hand_off` skips an identical hand-back already in the parent's history since its last reply |
| No visual check | New read-only tool `preview_file` renders an HTML/SVG/image file from the workspace in headless Chromium, served by a loopback server confined to the workspace (other origins and `file://` blocked); the workspace coding rules now ask for a render of visual changes |
| Tool images never reached the model | A round's tool images go to the model in one harness-sourced user message (Codex `input_image`), never persisted, older ones pruned to placeholders in batches |
| Dev CI red since 733836ea | `agentLoadouts.js` invalidates the shared settings cache after saving loadouts |

How each now works is in [`agent-runtime.md`](agent-runtime.md): "What
carries over between rounds" (§3) and "The brief, the writer and the
hand-back" (§6).

## Verification

- Full suite on Linux (WSL, CI env, `-n 12`): 10,566 passed, 196 skipped. Three
  failures, all addressed: two `test_shell_sandbox_probe` stubs that did not
  accept the new `extra_allowed` keyword, and the settings-cache test that was
  already failing on dev HEAD.
- `preview_file` was rendered for real with Chrome: relative and root-relative
  assets load; another loopback port, `file://` images and iframes are blocked.
  The proxy lockdown is verified on Windows Chrome only; check it on the
  container's Chromium.
- Not verified against the live backend: whether the Codex endpoint accepts
  `parallel_tool_calls`, `phase` on input messages, and `input_image` in a
  mid-run user message. The first two have a per-host fallback that drops the
  field after one refusal (grep the log for `rejected request field`);
  `input_image` has none, so a refusal there would show as failed rounds right
  after a tool returned an image. Codex CLI's `view_image` sends the same shape.

## Not done / open

- **Loadout text** (user data, not in the repo). The Lead Engineer and Admin
  instructions still carry the minimalism wording. A revised export was prepared
  for the user to import; it also raises the Lead Engineer's `max_rounds` to 150
  and grants `preview_file` and Penpot `render_preview`/`inspect_design`.
- **Skills** `technical-lead-orchestrator`, `agent-handoff-contracts` and
  `odysseus-agent-baseline` were not readable with the API token; they may carry
  the same wording.
- **Follow-up budget.** `agent_auto_continue_limit` (3) counts every launch, and
  once spent the hand-back tells the parent not to resume workers. With fewer,
  whole-feature workers this matters less; revisit if hand-backs still stall.
- **One orchestrator chat for everything.** The admin chat is ten days and 225
  messages old; every round re-reads ~190k tokens. A new chat per feature is
  faster and cheaper.
- `worktree_writers` does not register a worker resumed with `send_to_session`.
- The main checkout's `.git/hooks` and `config` are still writable from a
  sandbox whose workspace is the main checkout (the linked-worktree case is now
  guarded).
- The context profile's `reasoning_replay_rounds` is not read by the loop (the
  window is the constant 3).

## Evening follow-up (bundle 16:18–20:18, build ce04004b)

The fresh "Complete Agamemnon Theme Redesign" chat (gpt-5.6-sol) passed the
request verbatim to one Lead Engineer, built the three layouts, ran 59 tests
and opened PR #41. Rounds now batch 2–5 tool calls and write 300–1,100 output
tokens (reasoning carries over). The user still found it slow, and judged the
logo "extremely basic" and the sprites "the same image in different colors".

| Finding | Change |
|---|---|
| No ripgrep in the image: every `grep` spawned a Python worker that re-imported the app, ~12 s each (87 rounds, ~30 min) | `ripgrep` in the Dockerfile; the fallback runs `src/agent_tools/grep_worker.py` (stdlib only, still killable at the deadline); a one-time warning when rg is missing |
| Penpot advertises `penpotPublicURI = http://homelab.nas:9001`, which the container cannot resolve: every board render screenshotted Penpot's error toast and returned success, so no agent ever saw the mockup | `render_board` reads `/js/config.js`, opens the viewer at the public origin with `--host-resolver-rules` mapping it to the API host, and raises when the DOM shows Penpot's error page or error toast (checked against the NAS's real 2.17 DOM) |
| The Lead hand-wrote SVG for the logo and sprites | `visual-asset-sourcing` skill and a coding rule: real licensed artwork (Iconify game-icons etc.), distinct figures per identity, and the user picks between rendered options; `search_icons` takes `ids` and returns SVG markup with license and attribution |
| The Lead asked the read-only critic (no shell) to run tests: one run sat 15 min | `worker_preflight.shell_gap`: a task that runs commands is refused for a loadout without bash/python, with what to do instead; a delegation rule says run tests yourself and ask for one review per iteration |
| The critic's browser tools needed exact approval (`ask_risky`) inside a worker: three 5-minute stalls | Loadout change (user data): the critic runs `auto` |
| — | Community skills bundled (MIT, provenance in each SKILL.md and ACKNOWLEDGMENTS.md): grilling, writing-for-agents, unslop, diagnosing-bugs, improve-codebase-architecture (+ codebase-design, domain-modeling), triage, resolving-merge-conflicts |

Still to check after deploy: a `render_preview` of a mockup board returns the
board (not an error); grep rounds take well under a second.

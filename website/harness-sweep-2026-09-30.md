# Harness sweep — 2026-09-30

A pass over the prompts, the tool surface, the agent team flow and the UI,
checked against three days of the server's logs and against what vendors and
practitioners published in 2025–2026. What changed is in
[`agent-runtime.md`](agent-runtime.md) (§3, §5, §6) and
[`agent-worktree.md`](agent-worktree.md); this page keeps the evidence, the
comparison and what is left.

## What the logs showed (2026-09-27 → 2026-09-30)

From a diagnostics bundle (`/api/diagnostics/bundle?minutes=4320`): 2,284
model rounds, 3,719 tool calls, models gpt-6-luna, gpt-6-sol, gpt-5.6-sol and
gpt-5.5, all on the ChatGPT/Codex Responses route.

- **Prompt cache is healthy.** 79–84% of input tokens were cached on
  09-27…09-29 and 91% on 09-30, after the stable tool list (19be92d5). Rounds
  after a tool-list change re-sent 8.6M tokens uncached over the window, 8.3M
  of them before that deploy (09-29 ~21:00) and 0.3M after. Prep before the
  first model call is ~0.3–0.5 s.
- **Workers opened with a failed call.** Every `manage_git fetch` of the Umni
  checkout failed from 09-29 16:36 on (16 calls): its branch tracked a branch
  deleted after its PR merged, and the error named neither.
- **Workers in a managed worktree were sent the wrong way.** Six
  stage/commit/fetch calls there were refused with advice to start an isolated
  worktree, which is what they were already in; `diff` with the worktree's
  own path answered "'name' is required".
- **`manage_git diff` cannot compare against a ref** (two refusals for `ref`),
  and a diff over 64 MiB fails instead of truncating. Both remain open.
- **`discover_tools` was called 116 times**, each result printing the schemas
  it had just attached as up to 8,000 characters of JSON.
- **Guards rarely fire now.** Two checklist continues, one self-unblock, five
  duplicate-call skips and two round-budget wrap-ups in three days: the loop's
  nudges are a backstop, not the main mechanism.
- **Malformed native tool calls: none.** The audit found that such a call is
  dropped silently, with no result to the model; with zero occurrences in the
  window it stays on the backlog.

## What the model was actually told

The native-route prompt was mapped block by block. Findings (all fixed except the duplicate skills index, below):

| finding | effect |
| --- | --- |
| Tool schemas cut to the first ". " (≤ 220 chars), all parameter prose dropped | `bash` = "Run a shell command (full access)" (usually sandboxed, idle-timeout guidance lost); `ask_user`/`web_fetch` cut mid-word at "(e.g" |
| The stripper popped every key named `description` | the `description` parameter of `manage_calendar`, `manage_skills`, `manage_agent_loadout` and MCP tools vanished from the payload |
| "Local-machine mode" keyed on `ask_user`/`update_plan` | nearly every no-workspace agent turn was told the user "referred to this computer" and not to use email, calendar, notes or memory |
| Two skill indexes in Agent mode (not fixed) | ~3.5k chars twice per turn; the chat-route copy ignores the loadout's skill scope |
| Skills "proven to work, follow step by step", drafts "authoritative" | inside a wrapper saying not to follow instructions in the block |
| "Say what is missing" vs "discover it / do not claim you lack tools" | the base rule contradicted the routing note and the self-unblock check |
| ~22k chars of v1.0 rules still in the file | overridden at import, invisible, confusing to anyone editing prompts |
| An error beside tool output was never shown | a stopped command lost its reason |

## Research compared with the harness

Sources were read in full where possible; openai.com posts that returned 403
are marked (secondary) and were read through summaries.

| technique | source | Odysseus before | now |
| --- | --- | --- | --- |
| Leaner prompts, each rule once: +10–15% eval, −41–66% tokens | OpenAI, *Using GPT-5.6* prompt guidance | long domain blocks, duplicated rules, dead v1 set | base rules rewritten once each; v1 set removed |
| No shouting; give the reason — newer models over-trigger on CRITICAL/MUST | Anthropic prompting best practices; OpenAI GPT-5.5 guide; Codex CLI prompt (2 NEVERs) | ALL-CAPS in nudges | nudges and rules reworded with reasons |
| Stop rules in the prompt: "check your last paragraph; a promise is work to do now"; scope is the deliverable | Anthropic, *Prompting Claude Fable 5.1* | only as loop nudges | in `_API_AGENT_RULES`; nudges remain the backstop |
| Verify with a real check and report evidence; self-evaluation is lenient | Claude Code best practices; LangChain harness engineering (2026-02-17); Anthropic harness design (2026-03-24) | "verify every deliverable" | "check it the way the user would find out, say what you ran" |
| Tool descriptions carry when/why/side effects; description tweaks alone moved SWE-bench | Anthropic, *Writing effective tools for agents* (2025-09-11); OpenAI GPT-5.5 guide | truncated to one sentence on native routes | whole leading sentences + parameter prose |
| Actionable errors that say how to fix the call | same | git errors named neither branch nor fix | fetch falls back and says so; missing branch lists branches; managed-worktree refusal names the call |
| Stable, cached tool prefix; tools stay allowed once used | Manus context engineering (2025-07-18); OpenAI function calling / `allowed_tools` | already done (19be92d5) | — |
| Delegation brief: objective, output format, tools/sources, boundaries | Anthropic multi-agent research system (2025-06-13); Codex best practices ("Done when") | one-line task string | "Delegating to workers" rule block |
| Structured hand-back (intent, changes, decisions, next steps) | Factory, *Evaluating context compression* (2025-12-16) | free prose, truncated at 12k | Outcome / Changed / Checked / Open + Needs |
| One writer per worktree; parallelize reading and review | Cognition, *Multi-agents: what's actually working* (2026-04-22); Kim et al. arXiv 2512.08296 (−70% on sequential tasks) | not stated | in the delegation block |
| Status taxonomy with *Needs input* on top; notify on needs-input/done/failed | Claude Code agent view (2026-05); Magentic-UI | "Needs you" = tool approvals only; needs showed as Finished; hidden tabs silent | *Needs your input* status; hidden-tab notifications |
| Keep the plan visible while the agent works | Chen et al. arXiv 2604.04918 (plans cut problematic actions, OR 0.24); Claude Code todo list | checklist only in localStorage | live checklist above the composer |

## Backlog, ranked

Not done in this pass, in order of expected value:

1. **Verify `phase` replay on the Responses route.** OpenAI's Codex guide says
   models from gpt-5.3-codex on label assistant messages with `phase`
   (commentary / final_answer) and that dropping it on replay causes
   "significant performance degradation"; `build_responses_input` does not
   replay it. The loop now logs `[responses-phase] model=… emits message
   phase=…` once per model when the backend sends it. If that line appears for
   gpt-6-luna/sol, capture the phase with the round text and replay it.
2. **A clean-context reviewer before publish.** Cognition reports a
   diff-only reviewer catching ~2 bugs per PR, 58% severe; Anthropic found
   self-evaluation lenient. `agent_verifier_subagent` exists but is off; ship a
   read-only reviewer loadout that sees the brief and the diff, and run it
   between `commit` and `request_publish`.
3. **Structured launch fields.** Make the brief fields (`goal`, `done_when`,
   `starting_points`, `out_of_scope`) parameters of `manage_agent_loadout
   start`, rendered into the worker's first message, instead of prose guidance
   only.
4. **Worktree progress file.** Anthropic's long-running harness and Factory
   Missions keep `progress.md` / a feature list in the worktree so a resumed or
   replacement worker starts from it rather than re-deriving; resume the same
   worker by default.
5. **`manage_git diff` against a ref** and truncate-and-report instead of
   failing over 64 MiB.
6. **Harness directives as developer-role messages** on the Responses route
   (Codex sends environment and permission text as `developer`). Anthropic
   notes that frequent harness text in the user role can read as prompt
   injection.
7. **Tool consolidation**: `todowrite` into `update_plan`; single-message email
   tools into `bulk_email`; `manage_agent_worktree repo_*` into `manage_git`;
   `session_id` meaning a tmux session in three Cookbook tools; about 50 bare
   "Invalid JSON arguments" / "Unknown action" errors through one helper that
   names the valid shape.
8. **Report a dropped native call to the model** (bad JSON, missing required
   argument, unknown name) instead of dropping it; zero occurrences in the
   window, so low priority.
9. **One skills index, and an explicit direct-reply rule.** The chat route's
   unscoped skills index duplicates the agent loop's scoped one, but removing
   it changed behaviour: it is a user-role envelope, `_user_turn_count` counts
   envelopes, and so it is what keeps every Agent-mode first message off the
   tool-free direct reply path. With it removed, "Have the Lead Engineer add
   double(x) … and run the tests" was classified low-signal
   (`_looks_like_a_request` misses "have X do Y") and answered with no tools;
   the e2e harness failed 8 checks and the change was reverted. Fix the
   classifier for delegation phrasing and state the direct-path rule outright
   (for example: never on a first message that names a loadout or a tool),
   then drop the copy.
10. **Claude API routes**: Anthropic now rejects edits to earlier turns for new
   accounts (from 2026-08-31, per the Fable 5.1 guide); the execution ledger
   rewrites old tool exchanges in place. Before using Claude over the API
   again, move that route to server-side context editing.

## Loadout notes (user data, not changed)

The server's Lead Engineer loadout sets `max_tokens: 9000` and
`temperature: 0.2`. On the ChatGPT route neither is sent (the Codex backend
takes no `max_output_tokens`, and gpt-5.x/6 restrict temperature), so they only
matter if the loadout runs on another provider, where 9,000 output tokens can
cut a reasoning model off before its report. Its instructions contain two typos
("ree request_publish", "userapproves") and a proportionality paragraph that
the harness's delegation rules now also state. These live in the server's
settings, not in this repository.

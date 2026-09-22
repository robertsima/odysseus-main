# Upstream sync, 2026-09-18: what changed and what still has to be re-ported

This sync redoes the upstream merge that PR #25 attempted (reverted in `ab232825`).
The chosen foundation is **upstream's agent core**: `src/agent_loop.py` and
`src/tool_approvals.py` are upstream's, and the fork's work on top of them is
re-ported feature by feature in follow-up PRs. Everything else is a real
three-way merge, including `src/tool_execution.py` and
`src/agent_tools/filesystem_tools.py`. Those two carry the fork's security
boundaries, so taking upstream's copies would have removed protections rather
than features.

## Kept in this PR

These are enforced at execution time, so they hold whatever the loop offers
the model.

- **Private vault boundary** (`da53f2c4`):
  - Documents labelled private are denied to every file tool: read, write,
    edit, patch, ls, glob and grep.
  - `bash`, `python` and MCP filesystem tools are refused without the chat's
    private-vault grant.
  - The grant flows from the route through `stream_agent_loop(allow_private=...)`
    into `execute_tool_block`, where it is re-checked against fresh session
    settings. `allow_private` and `steer_run_id` (below) are the only
    additions to upstream's loop signature.
- **Protected paths** (`7f3edbd3`, `8a7e6dc2`): the file tools refuse:
  - the agent worktree's approval state, so an agent cannot forge a publish grant,
  - private key files, by extension,
  - the configured signing key.
- **Capability-profile policy**: session settings are re-read on every tool
  call, so a profile or tool change takes effect mid-turn.
- **Lotus privacy**: MCP Lotus calls from an endpoint the owner has not
  approved are refused.
- **Rounds never end a run** (`331c996c`). This is ported into upstream's loop:
  `max_rounds` is advisory and the loop-breaker bounds a run. The fork's
  `MAX_AGENT_ROUNDS = 0` would otherwise have given upstream's
  `range(1, max_rounds + 1)` zero rounds.
- **Steer and agent-to-agent message delivery** (`faf3ee0a`, `a5322720`):
  - Upstream's loop now drains the steer queue at the top of every round.
  - The queue is keyed by `steer_run_id`, which the chat route and headless
    workers pass in.
  - A peer agent's message goes in with its own attribution; the user's is
    labelled as a mid-task instruction.
  - Without this, `message_agent` and the Steer box queued messages that
    nothing ever read.
- **Native tool calls over the ChatGPT Responses API** (`bd83989e`). Nothing
  needed porting: `llm_core` emits the same `tool_calls` event upstream's loop
  reads.
- **ChatGPT/Codex models get tool schemas**: `chatgpt.com` is in upstream's
  `_API_HOSTS`. Without it these models were sent no tools and waited for
  prose tool blocks they never write.
- **The request is the last thing the model reads** (`78174368`): the chat
  builder appends retrieval and time context after the human turn for prompt
  caching, and upstream's prompt builder now moves that trailing context back
  in front of the request.
- **Bundled skills don't arm the approval gate**:
  - Upstream wraps skill text as untrusted and arms the exact-approval gate
    whenever skills are shown. The fork seeds bundled skills on every install,
    so that would gate every turn.
  - The gate is skipped only when every skill shown is unmodified shipped
    content (`src/builtin_skills.is_shipped_skill`). Checking the
    `source: bundled` label isn't enough, because the seeder keeps an edited
    body.
  - Any skill a user or agent wrote or edited still arms the gate.
- **Explicit capability classes for the fork's tools** in
  `src/tool_capabilities.py`, following upstream's closest equivalents.
  `manage_git`'s history rewrites and discards are marked destructive.
- **Workspace fallback keeps its refusals**: the personal-docs second chance in
  `_resolve_tool_path` applies only to paths that are merely outside the
  workspace. It no longer masks an application-state or sensitive-path
  refusal.
- **Exact `skills.sh/<owner>/<repo>/<skill>` links** map to GitHub without a
  network fetch (fork). Other skills.sh links take upstream's checked
  redirect path.
- **Pure helpers moved out of the fork's loop**:
  - `src/delegation_intent.py` holds the delegation recogniser. Workflow
    launch authorization uses it.
  - `src/skill_toolsets.py` resolves a skill's `requires_toolsets`. The
    manage_skills author warning uses it.

## Re-ported since

- **Approval modes** (`a3a153e3`, `3c8c2654`). `src/approval_modes.py` now
  drives upstream's gate through `ToolRunSecurityContext.approval_mode`:
  - `auto` never asks, `ask_risky` asks for destructive and outward-facing
    calls, and `ask_all` asks for every change and keeps the untrusted-context
    gate as well.
  - The chat route, headless sub-agents, background-job follow-ups and
    scheduled tasks pass the chat's mode, else the app default
    (`agent_approval_mode`). A run with no mode (the skill tester, teacher
    escalation, API-token runs) keeps upstream's untrusted-context gate.
  - A mode approval uses upstream's exact-action card. It carries no
    untrusted context, so the executor accepts it in an unarmed run.
  - A sub-agent's card is saved in its own chat and listed in the Agents
    panel, with the reason it asked.

## Added to upstream's loop since

- **Terminus toolset retention** (`f8882905`, `3e2a0eb5`, `5181684e`).
  `apply_terminus_toolset()` merges the local-machine toolset into the turn's
  selection instead of replacing it whenever the user's own words also named
  an assistant domain, and on the replace path it still carries across
  whatever retrieval matched for this query (MCP tools included). The
  `on|from <bare word>` branch of `_LOCAL_COMPUTER_REFERENCE_RE` now requires
  the token to look like a host, so "confirmations from Gmail" is no longer
  read as work targeted at a machine called Gmail. `audit_emails` was also
  missing from the `email` domain map, leaving deterministic seeding with no
  path to the whole-mailbox report tool.

  Without these the scheduled "Applied Job Status Tracker" run ended with
  "the email-audit tool/schema is not available in this run" (2026-09-19
  logs). Covered by `tests/test_terminus_toolset_retention.py`; the rest of
  `tests/test_agent_self_blocking_toolsets.py` (missing-tool re-arm,
  starved-domain repair) stays skipped and on the backlog.

- **Tool budget** (`agent_tool_budget`, default 40). Keyword domain seeding
  can offer most of the catalogue on a long message (80 tools, ~66k prompt
  tokens in the 2026-09-18 logs). Past the budget,
  `_apply_tool_budget` drops whole seeded domains, starting with the ones
  retrieval agrees with least. Retrieved, forced, document, upload and
  skill-required tools always stay.
- **Result-aware loop-breaker**. A repeated call counts toward the stall
  streak only if its result is unchanged too (`_result_progress_digest`; a
  tool can name its own `progress_key`). A polled Claude Code job that shows
  new activity is progress, and one that shows only a larger elapsed time is
  not. Repeat polls also wait on the job (30s, doubling, capped at 240s)
  instead of returning at once.

- **Same-turn tool attachment** (2026-09-22). `discover_tools` works again:
  the loop builds a `TurnToolDiscovery` over native and MCP schemas, hands it
  to the executor, and attaches what it loads to the next round
  (`continue_same_turn: true`). It is always offered, including on
  caller-provided selections. A deterministic, exact-name form of the
  missing-tool re-arm: a short final answer that says it lacks a named,
  permitted tool gets that tool attached and one more round, at most twice a
  turn (`_missing_tools_to_attach`). The fork's prose-shape detectors and
  starved-domain repair are still on the backlog. Covered by
  `tests/test_same_turn_tool_attachment.py`.
- **Skill routing through `skill_declared_tools`**. Matched skills, a skill
  loaded with `manage_skills view`, and a profile's selected skills resolve
  `requires_toolsets` by exact name, MCP server name, or alias, instead of a
  bare known-name match that dropped `todoist`, `lotus` and server names. A
  chat's `skill_access`/`skill_names` now scope the skill index and matched
  procedures, and a profile's selected skills bind their tools from round one.
- **Offer matches enforcement**. The loop applies the chat's saved tool
  policy (`session_policy_disabled_tools`) whichever caller started the
  turn, and adds tools the user names outright.

## Changed behaviour until re-ported

| Area | Now | Fork commits to re-port |
|---|---|---|
| Fork approval store (once/always grants, reissue, precheck before hold) | Replaced by upstream's `ToolApprovalStore`. The Agents overview lists pending approvals and links to the chat, where the card is decided. | `a3a153e3`, `3d8ed0fc`, `8927810b` |
| `manage_git` risky actions | No per-call confirmation of its own. It is classified as a workspace write with network and external side effects (destructive for rewrites and discards), so upstream's exact-approval gate holds it once a run is tainted. Pushing this repository is still refused (`use_publish_flow`). | `3c8c2654`, `3d8ed0fc` |
| Approval prompts from skills (runs with no approval mode only) | A turn that shows any skill a user or agent wrote or edited arms upstream's gate, so the next high-impact call (bash, writes) asks for an exact approval. "Allow for this chat session" covers the rest of that chat. | Upstream design, kept |
| Per-agent profile instructions (`agent_instructions`) | Stored, but not placed into upstream's system prompt | `0ea6b80b`, `f2f9f83e` |
| Tool routing: intent classes, domain routing, targeted self-unblock, protected admission budget, missing-tool re-arm, starved-domain repair | Upstream's selection | `8927810b`, `c62b6bac`, `cffc5f0d`, `d97fa0e7`, `32614be0`, `a654a09e`, `3e2a0eb5`, `f8882905`, `fc74bf51`, `5ee56c0a` |
| Continuation ("ok, continue" keeps the last turn's tools) | Upstream's handling | `8927810b`, `00b35dbd`, `a364f8f2` |
| Delegation in the loop: policy gating, standing `delegation_granted`, plain-language "start an agent" routing | Workflows still check authorization. The loop does not hide or route delegation tools. | `ced11e62`, `7c9c03a2`, `16ad4e47`, `0ea6b80b` |
| Steering beyond delivery: tools added because of a steer, and continuing a turn that would end while a steer is pending | Steers and peer-agent messages are delivered between rounds (see Kept). A steer that arrives after the last round is dropped with a visible `steer_dropped` event. | `75988e56`, `4c4ce597` |
| Execution ledger (`recall_tool_output` references) | The ledger is not written, so there is nothing to recall | `6131859d` |
| Prompt and schema efficiency: stable prefix, schema ledger, cache-shard affinity, reasoning replay, context accounting | Upstream's | `97b6691c`, `913a605d`, `f4bdeed2`, `c129bbcc`, `8cca5a1e`, `d47e5160` |
| Schema-level hiding: private-grant tools, Lotus, loadout-disallowed tools | Offered to the model but refused at execution | `da53f2c4`, `0ea6b80b`, `cabbe6d1` |
| Git, research and MCP routing prompts | Upstream's | `e16d1ec5`, `94fa0d17`, `982e60eb`, `9f7062d0`, `836eb7d8` |
| Knowledge-base and vault routing | Upstream's | `68646700`, `8dfacd08`, `4360acf2`, `98a4e3ff` |
| Request self-heal, `site:` handling | Upstream's | `0611fa1e` |

The fork tests for these features are skipped, at module level or per test,
with a `Re-port backlog:` reason. Each follow-up PR re-enables the tests for
the feature it brings back. `tests/test_agent_loop_fork.py` is the fork's
whole loop suite, kept for reference while `tests/test_agent_loop.py` is
upstream's.

Found along the way, not changed here: the bundled-skill seeder rewrites each
installed `SKILL.md` through `Skill.to_markdown()`, which drops some section
headings and the free-form body text (`body_extra`).

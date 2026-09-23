# The agent runtime

How a turn in agent mode actually runs: how the tool list for a round is
chosen, what the prompt is made of, how context is kept from growing without
bound, and what the loop does when the model gets stuck.

This is the *mechanism*. [`harness-operating-model.md`](harness-operating-model.md)
is the *contract* — what a good turn looks like from the user's side. When the
two disagree, the operating model states the intent and this file states what
the code currently does; fix whichever is stale.

Most of what follows lives in `src/agent_loop.py`. It is a large file; the
section headings below name the functions so you can jump rather than read.

---

## 1. The shape of a turn

A turn is a loop of **rounds**. Each round is one request to the model, plus
the execution of whatever tools it asked for.

```
  user message
       |
       v
  classify intent ......... _classify_agent_request
       |
       v
  select tools ............ retrieval + hints + domains + retained
       |
       v
  build prompt ............ _build_base_prompt
       |
  +----+------------------------------------------------+
  |    v                                                 |
  |  assemble schemas ...... _tool_schemas_for_round      |
  |    v                                                 |
  |  call the model (streamed)                           |
  |    v                                                 |
  |  parse tool calls ...... native function calls or     |
  |    |                     text fences                  |
  |    v                                                 |
  |  execute tools ......... src/tool_execution.py        |
  |    v                                                 |
  |  append results ........ _append_tool_results         |
  |    |                     (+ execution ledger)         |
  +----+ more tool calls? ---+                            |
       |                                                  |
       v no                                               |
  final answer -------------------------------------------+
```

The loop ends when a round produces an answer with no tool calls, when a guard
stops it (§5), or when the round budget runs out.

**Round budget.** A chat turn uses the `agent_max_rounds` setting (default
100, clamped 1–500). A worker launched from a loadout uses that profile's
`max_rounds` instead — `agent_profiles.py` defaults to 12 and clamps to 1–40.
The gap is deliberate and worth knowing: a worker that stops at round 12 is
usually at its loadout's budget, not at a bug. Hitting the cap mid-task emits
`rounds_exhausted`; a live chat turns that into a Continue button, and a
headless worker returns its partial work and records the run `incomplete`
rather than `completed` (`src/agent_control.py::launch_worker`).

---

## 2. Choosing the tools for a round

The registry holds on the order of 140 tool schemas once MCP servers are
connected. Sending all of them every round is expensive and — more importantly
— misleading: an unselected schema is an active suggestion about what the turn
is for. A turn that was handed a design tool and no log-reading tool spent six
rounds steering the design tool.

So each turn selects a subset. Selection is a union of several sources, and
the `[tool-routing]` log line reports which one dominated.

### 2.0 Pinned toolsets

None of that runs when the session's own policy has already named the toolset.
A loadout with `tool_access="selected"` reaches the loop as its *complement* —
`agent_profiles.session_patch` stores every known tool minus the enabled ones
in `disabled_tools` — so `_pinned_policy_toolset` reads the allowlist back out
of the denies and binds it whole whenever it holds no more than
`agent_pinned_toolset_max_tools` (25) tools. Retrieval, the embedding call and
domain seeding are skipped, and `_reassert_pinned_toolset` puts the set back
after the shaping passes that run for every selection (§2.2), so the schema
list is byte-identical across the rounds of a turn and across turns and the
cached prefix holds. `[tool-routing] source=pinned` says it happened.

Before this, a role's bound set varied per turn *inside* its own allowlist: one
session's schema block walked 3846 → 4246 → 4500 → 4787 → 5418 tokens across
consecutive turns, and `use ntfy to send a notification to odysseus` retrieved
21 tools including the whole email suite, because the index has no similarity
floor (§2.2). Computing a subset of a declared list, differently each turn, is
work that can only make the answer worse.

Two consequences worth knowing. A pinned role's missing-tool self-unblock (§5)
finds nothing to re-arm — the pin already holds every builtin the policy allows
— so the round lands in the "nothing left to re-arm" branch, which is the
honest answer: what it is asking for is denied, not merely unselected. Gated
MCP catalogs are the exception and still re-arm, because an allowlist expressed
over `known_tool_names()` never denied them.

### 2.1 Intent classification

`_classify_agent_request(messages, last_user)` returns:

| field | meaning |
| --- | --- |
| `domains` | subject areas detected in the message (`files`, `email`, `self_diagnosis`, …) |
| `continuation` | this turn continues established work rather than starting something |
| `low_signal` | there is nothing here worth retrieving against |
| `retrieval_query` | the text used for embedding retrieval |

`low_signal` is the important one: a low-signal turn skips embedding retrieval
entirely and falls back to keyword hints. It is computed as

```python
low_signal = not continuation and not domains and not _looks_like_a_request(text)
```

The third term exists because domain coverage alone was doing a job it was
never designed for. Any wording outside the enumerated domain regexes —
"fix the failing test", "why did that fail" — was landing in the same bucket as
"hey", so the embedding index was built, paid for, and then bypassed on most
turns. `_looks_like_a_request` is the positive mirror of `_is_casual_low_signal`:
an action verb or a question, *plus* at least one content word. It is ANDed in,
so it can only ever clear `low_signal`, never set it.

### 2.2 Retrieval and hints

- **Embedding retrieval** (`src/tool_index.py`) scores the query against tool
  descriptions, including every connected MCP tool. Skipped on low-signal turns.
- **Keyword hints** (`_KEYWORD_HINTS`) map literal phrases to tools. Matched on
  word boundaries in both the index pass and the loop's fallback pass — these
  two had drifted, and a raw-substring match meant "local **pr**oject" pulled in
  the source-control toolset.
- **Domain seeding** adds each detected domain's tools from `_DOMAIN_TOOL_MAP`.
- **Retained tools** carry over tools already used in this conversation, subject
  to the retention policy in
  [`specs/personal-directories-and-tool-routing.md`](../specs/personal-directories-and-tool-routing.md):
  generic execution tools (`bash`, `python`, `run_shell`, `shell`) are not
  retained by default.
- **Admin intent** (`_detect_admin_tools`) maps management vocabulary to the
  specific management tools those words point at — not the whole admin set.
  Orchestration phrasing ("kick off an agent", "run some agents", "delegate this
  to claude code") routes here, to the delegation tools.

### 2.3 Gates that subtract

Selection is then narrowed. Each gate has a reason, and a tool dropped after
retrieval matched it is reported in `[tool-routing]`'s `dropped_query_matches`
with the gate responsible — without that, a tool vanishing between retrieval
and selection was invisible.

| gate | effect |
| --- | --- |
| delegation policy | `never`, or `explicit` without an explicit request, disables the delegation tools — including `manage_agent_loadout`, whose `start` action launches a worker. It cannot reach a remote-execution MCP tool; that is `mcp_access`'s job, and `_DELEGATION_TOOLS` says why |
| model / memory / skill access | the chat's loadout (§6) removes what it is not allowed |
| plan mode | allowlist of read-only tools only |
| owner baseline | the operator's global `disabled_tools`, the user's privileges, and the non-admin blocklist |
| capability availability | a tool whose subsystem is not installed or configured is withheld |

The owner baseline (`src/tool_security.py::owner_baseline_disabled_tools`) is
the single merge point every agent turn passes through, whichever route started
it — a chat turn, a `send_to_session` sub-agent, a background follow-up.

### 2.4 Assembling the schema list

`_tool_schemas_for_round` turns the selection into the exact list sent. Three
things are in the payload that were not selected, each deliberate:

1. **Small connected MCP servers.** A server the user explicitly wired up stays
   bound every round, so it does not vanish the moment a follow-up ("continue",
   "now run it") fails to resemble its description. Bounded by
   `src/mcp_manager.py`: **8 tools per server**, **24 in total**, trimmed
   largest-server-first. A server over the limit is *demoted* — gated behind
   retrieval like the built-in catalogs, still indexed, still reachable. Both
   caps are settings (`mcp_always_bound_server_max_tools`,
   `mcp_always_bound_total_max_tools`); 0 disables that dimension.
2. **Admin tools the request's keywords named**, when admin intent fired.
3. **The whole registry**, only when retrieval itself was unavailable. Gating
   there would make a connected server unreachable for the turn with no way
   back.

A gated or demoted server keeps its full tool listing in the prompt with a note
that its call schemas are not attached this turn and how to ask for one. The
listing is also what `ToolIndex` embeds, so dropping it would make those tools
unretrievable as well as uncallable.

The `[agent-debug]` line reports `tools_sent`, `selected`,
`schema_without_selection` and `mcp_demoted` for exactly this audit.

---

## 3. What the prompt is made of

`_build_base_prompt` assembles, in order: the operating instructions, the tool
sections for the selected tools, any loaded skills, local context (workspace,
active document, personal directories), and the connected-MCP listing.

Two properties matter more than the contents.

**Prefix stability.** Providers cache a request's prefix. Editing the *middle*
of an already-cached prompt moves the cache boundary and costs more than it
saves. [`specs/prompt-prefix-stability.md`](../specs/prompt-prefix-stability.md)
documents a turn where pruning replayed reasoning items every round invalidated
the prefix continuously. The rules that came out of it apply to anything that
edits history mid-turn:

- prune and compact in **batches**, so invalidation happens once per window
  rather than every round;
- deliver mid-turn directives as a labelled **user**-role message at the tail
  (`_harness_directive`), never `role: system` — `llm_core` hoists every system
  message into the single instructions block, rewriting the front of the prefix.

**Untrusted content is fenced.** Retrieved documents, memories, web pages, tool
output and skill text are reference data, not instructions. The fence is
required by [`THREAT_MODEL.md`](../THREAT_MODEL.md) and reproduced verbatim by
anything that rewrites a tool result.

---

## 4. Keeping context bounded

Three mechanisms, in increasing order of severity.

### 4.1 Tool-output offload

A large tool result is written to `src/tool_output_store.py` and replaced with
an excerpt plus a `toolout-…` reference. `recall_tool_output` retrieves the
full text on demand. The inline limit comes from the context profile
(§4.3).

### 4.2 The execution ledger

`src/context_compactor.py` collapses *completed* tool exchanges into a compact
record: what ran, on what, and with what outcome — dropping the bulk.

It leans on a structural property of `format_tool_result` rather than a
judgement call: **facts live outside fenced blocks, bulk lives inside them.**

~~~
### read_file: /srv/app/src/agent_loop.py      <- fact (what, and on what)
**content (31204 chars):**                     <- fact (shape)
``` ...31k chars of source... ```              <- bulk, replaced by a summary line
**exit_code:** 0                               <- fact (outcome)
~~~

A path or id the agent would otherwise re-derive by re-running a tool is never
inside a fence, so it is never dropped. The original is written to the output
store *before* rewriting; if that write fails, the exchange is left verbatim.

Never collapsed: the most recent `LEDGER_KEEP_ROUNDS` (3) exchanges; results
under `LEDGER_MIN_RESULT_CHARS` (600), which are all fact and no bulk;
`recall_tool_output` results; `ask_user`; and failures, until a later round runs
the same tool successfully.

Batching: exchanges accumulate to keep (3) + slack (4) before anything is
touched, so the prefix is invalidated about once every five rounds rather than
every round. Entries are append-only and never rewritten, so the invalidation
point advances toward the tail.

Kill switch: `ODYSSEUS_AGENT_EXECUTION_LEDGER=0`.

### 4.3 Context profiles and trimming

`src/context_profiles.py` picks a profile from the model's window —
`compact` below 32k, `long_context` at 200k and above, `balanced` between — and
that profile sets the inline tool-output limit and the trim target.

The window itself is resolved per model. A headless worker resolves its own
(`src/headless_agent.py`); it used to read a `context_length` attribute that
`core.models.Session` does not have, so every worker ran at `preset_for_window(0)`
— `balanced` — and truncated tool output at half what a chat on the same model
got.

Trimming (`trim_for_context`) is the last resort: it *drops* messages, where
the ledger keeps their facts and a recall pointer. Automatic compaction is
governed by [`specs/bounded-recursive-compaction.md`](../specs/bounded-recursive-compaction.md) —
prior summaries are replaced by the consolidated one, never accumulated.

---

## 5. Guards in the round loop

The loop watches for characteristic ways a model gets stuck. Every guard is
bounded — a guard that can fire indefinitely is a new way to burn the budget.

| guard | trigger | response | bound |
| --- | --- | --- | --- |
| duplicate call | same tool, same arguments, already run this turn | answer from the memo; tell the model once | `_MAX_DUP_CALL_DIRECTIVES` = 2 |
| missing-tool self-unblock | the round claims it lacks tools or access | re-arm the tools the claim points at | `_MAX_TOOLSET_REARMS` = 1 |
| intent without action | the round announces an action and calls nothing | one sharp nudge to actually call it | `_MAX_INTENT_NUDGES` = 2 |
| runaway call | the same call signature very many times | stop the turn | fixed |
| stall | repeated identical rounds with no answer text | stop the turn | fixed |
| round cap | budget spent with work outstanding | emit `rounds_exhausted` with partial work | — |

### Duplicate calls

The memo is keyed on the tool name plus the **full** canonicalised arguments —
a 120-character prefix would collide two long `bash` scripts that differ at the
end. Three exemptions, because a false positive here withholds a call the model
genuinely needed:

- **Polling is exempt.** Recognised by an allowlist, by *name shape*
  (`status`, `poll`, `check`, `wait`, `progress`, …, matched against the bare
  name so `mcp__x__job_status` works — MCP names carry a per-server hash and can
  never be enumerated), and by the **`action` in the arguments**, because
  `delegate_to_claude_code {"action": "poll"}` polls under a name with nothing
  poll-shaped in it. Missing any of these three makes a job impossible to
  observe finishing.
- **A mutating call clears the memo**, so a re-read after an edit is correct.
  Recognised by the same three routes.
- **Only successful results are memoised.** An approval hold is never recorded:
  the approval contract requires re-issuing byte-identical arguments.

### Missing-tool self-unblock

A round that ends with "I don't have the tools for that" is almost never
telling the truth — the tool exists; selection just did not put it in this
round's list. Two detectors:

- a narrow **regex** for claims about tools or access being unavailable;
- a **structural** signal: zero tool calls plus two or more enumerated
  capability negations ("- I do not have application-log access"). Wording
  varies endlessly; the shape does not. Three real refusals missed the regex in
  succession before this was added.

Recovery is **targeted**: the tools the claim actually names, resolved through
verbatim tool names, `_SKILL_TOOLSET_ALIASES` and the keyword hints, with
hyphens and underscores flattened so `application-log` matches `application log`.
If nothing is targeted, it widens to the **domains with evidence in this turn** —
never the whole registry. A full re-arm on a 140-tool install is roughly 14k
schema tokens per remaining round and invalidates the prefix cache; and a round
whose claims resolve to no tool name, alias, keyword, index neighbour or domain
is very likely correct that the capability is absent.

---

## 6. Loadouts, workers and delegation

A **loadout** (`src/agent_profiles.py`) is a named policy: instructions, model,
tools, skills, memory access, MCP access, delegation policy, approvals, worker
limit, round budget. It is a reusable starting point; the persisted per-chat
settings are the effective policy.

Starting a worker (`agent_control.launch_worker`) creates a fresh chat, applies
the profile via `session_patch`, and runs it headless and detached. Progress
appears on the worker's own activity feed and on its parent's.

An agent can author loadouts too (`manage_agent_loadout`). The rule that makes
that safe is in `src/agent_loadouts.py`: **every capability in a loadout an
agent creates is intersected with the calling chat's own policy**, and each
narrowing is reported back. A chat denied `bash` cannot mint a helper that has
it. This is an *authoring* rule — what a worker may actually do is still decided
at execution time by its own stored policy and the owner baseline.

### Steering

A human or a peer agent can queue a message for a turn already running; the
loop drains it at the start of each round. Every message carries a state
(`src/agent_control.py`):

```
queued -> acknowledged -> injected
              \-> cancelled      (turn ended before it was drained)
              \-> failed         (queue full, or refused, with a reason)
```

There is deliberately **no `completed`**. Nothing in the system observes a
steer being *carried out*: after injection it is one user message among many,
and the model's later output carries no back-reference. A `completed` written at
turn end would mean only "the turn ended" — the false assurance the states exist
to remove. Transitions are published to the activity feed, so a message is
inspectable long after the in-memory queue drained.

---

## 7. Reading the logs

A turn's behaviour is reconstructable from these lines.

| line | answers |
| --- | --- |
| `[agent-intent]` | what the turn was classified as, and which tools were selected |
| `[tool-routing]` | which source dominated, what was dropped and by which gate |
| `[agent-debug]` | how many schemas were sent vs selected, and which MCP servers were demoted |
| `[context-profile]` | the profile, inline limit and trim target in force |
| `[agent-timing]` | prep breakdown, per-round elapsed, time to first token |
| `[agent-usage]` | input / cached / output tokens per round |
| `[agent] missing-tool self-unblock` | that recovery fired, with scope and which detector |
| `[tool-output] offloaded` | a result moved to the output store, with its recall id |

Two habits worth keeping when adding to them: report the **difference**, not a
truncated list (a list clipped at N answers "was my tool sent?" confidently and
wrongly), and state truncation inside the string when it happens — `_name_list`
exists for this.

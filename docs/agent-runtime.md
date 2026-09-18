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

**Round budget — there isn't one.** A round count never ends a run, for a chat
turn or for a worker. Every incarnation of a ceiling did the same thing: stop an
agent in the middle of a task it was still working on and hand back half of it.
Raising the numbers only moved where that happened, so the mechanism is gone.
`max_rounds` (and `agent_max_rounds`) are still accepted and still reported for
display, but they are advisory — `stream_agent_loop` iterates until the work is
done.

What bounds a run instead measures PROGRESS rather than counting iterations, and
is strictly better at the job: the loop-breaker's stall detector (four rounds
with no new call and no new text), the runaway detector (the same call with the
same arguments repeated), the per-run tool-call ceiling (`agent_max_tool_calls`,
default 500), the request timeout, each tool's own policy, and the user's stop
control, which stays live for the whole run. An agent that is genuinely working
runs until it finishes; one that is stuck is caught by the thing that can
actually tell it is stuck.

`agent_control.CONTINUATION_LEGS` remains as the recovery path if a ceiling is
ever reintroduced: a worker that exhausts a budget while still executing tools
is handed another, carrying its previous leg's work and an instruction not to
redo it, and a leg that executes no tools earns nothing.

---

## 2. Choosing the tools for a round

The registry holds on the order of 140 tool schemas once MCP servers are
connected. Sending all of them every round is expensive and — more importantly
— misleading: an unselected schema is an active suggestion about what the turn
is for. A turn that was handed a design tool and no log-reading tool spent six
rounds steering the design tool.

So each turn selects a subset. Selection is a union of several sources, and
the `[tool-routing]` log line reports which one dominated.

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
  descriptions, including every connected MCP tool. Skipped on low-signal turns,
  and neighbours below the conservative similarity floor are discarded instead
  of padding the selection with unrelated tools.
- **Keyword hints** (`_KEYWORD_HINTS`) map literal phrases to tools. Matched on
  word boundaries in both the index pass and the loop's fallback pass — these
  two had drifted, and a raw-substring match meant "local **pr**oject" pulled in
  the source-control toolset.
- **Email context** is shared by retrieval, keyword fallback and domain
  seeding. Generic "send/message/reply" wording does not buy the mailbox suite.
  An email-subsystem audit routes as source work; genuine mailbox queries,
  explicit recipient addresses and mixed audit-plus-email requests retain
  their tools. Read-only mail discovery does not add mutation schemas merely
  because they were nearby in the embedding index. Shared contact/UI tools and
  explicitly named tools remain eligible.
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
| delegation policy | `never`, or `explicit` without an explicit request, disables delegation launchers, loadout creation and qualified MCP `run_pi_task` tools |
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

Explicit server names now add a bounded, deterministic read-only selection even
when embeddings miss. For example, an eleven-tool Bluesky server can contribute
its profile/timeline/post reads without attaching posting tools. Deliberate
per-agent `enabled_tools` bindings are selected even on a vague follow-up.
`tool_access=selected` is persisted and enforced at execution time, so a newly
connected MCP tool cannot evade an older denylist snapshot. Disabled tools,
private access and MCP server restrictions still win. A destructive annotation
overrides a contradictory read-only annotation.

The `[agent-debug]` line reports `tools_sent`, `selected`, `admin_selected`,
`schema_without_selection` and `mcp_demoted` for exactly this audit. A hints-only
selection is labelled that way rather than being reported as embedding retrieval.

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

`[agent-cache]` diagnostics log privacy-safe hashes of the static system/schema
prefix and compare each request history with the prior round. They report only
the first changed message index and short old/new hashes, so an accidental
mid-history rewrite is visible without logging prompt content.

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

### 4.4 Discovery and learning overhead

`app_api` endpoint discovery is paged (default 25, maximum 50, with an additional
serialized-size bound). `filter`, `offset` and `next_offset` let the model narrow
or continue a result without receiving the entire API twice as prose and JSON.
Loadout listing shows compact summaries; `get` returns a named profile's full
policy and instructions. `capabilities` uses counts/examples by default and
`detail=true` exposes the full tool ceiling.

Automatic skill extraction uses recorded tool outcomes. Launch-only,
approval-held, failed or duplicate-only turns skip the teacher request. Eligible
turns include bounded outcome metadata (not raw tool output or arguments), so a
promise to launch an audit is not mistaken for a proven audit procedure. Learned skills remain guidance to verify,
not an assertion that their outcome has already been validated.

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
- **A mutating call clears cached results**, so a re-read after an edit is
  correct. Recognised by the same three routes. Its own same-signature failure
  counter is retained so a repeatedly failing mutation cannot evade the guard.
- **A failed call gets one real retry.** The second identical failure is retained
  and the third call is suppressed with the prior error quoted to the model.
  Approval holds are never counted or memoised: the approval contract requires
  re-issuing byte-identical arguments.

### Missing-tool self-unblock

A round that ends with "I don't have the tools for that" is almost never
telling the truth — the tool exists; selection just did not put it in this
round's list. The detectors are:

- a narrow **regex** for self- or session-scoped claims about tools, capabilities
  or access being unavailable, including phrases such as "calendar access isn't
  available in this session" while excluding upstream-service outage reports;
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

Worker capacity counts queued as well as running Claude Code jobs, deduplicated
against their activity records. An empty settings record still uses the default
one-worker limit. Capacity gates new work; listing, polling and cancelling
existing delegation remain possible at capacity. Blocked launches expose
`blocked_reason=worker_capacity` and current limits so the caller can report what
actually started. Provider acknowledgements without a trackable task are
reported as unconfirmed, and provider failures cannot be reported as completion.

There are two independent concurrency controls. **Child workers for this agent**
is the parent chat's permission ceiling (`max_parallel_workers`, default 1,
range 0–8). **Provider-wide concurrent jobs** is Claude Code's shared process
capacity. Increasing provider capacity to eight does not grant each parent
permission to launch eight children. Capacity refusals name the parent-chat
scope and its setting; monitoring and stopping remain available at capacity.

An agent can author loadouts too (`manage_agent_loadout`). The rule that makes
that safe is in `src/agent_loadouts.py`: **every capability in a loadout an
agent creates is intersected with the calling chat's own policy**, and each
narrowing is reported back. A chat denied `bash` cannot mint a helper that has
it. This is an *authoring* rule — what a worker may actually do is still decided
at execution time by its own stored policy and the owner baseline.

Losing *every* requested tool is not a narrowing, it is the loadout failing to
exist: the clamp used to store that as `tool_access: "none"`, and each worker it
started opened by saying it was blocked. Such a loadout is now refused at
create/update, naming the tools the chat can actually grant, and a stored one is
refused at `start` with the loadouts that do have tools. `start` returns the
model, tool bindings and round budget the worker actually got, so a wrong-fit
loadout is visible immediately rather than after the worker reports it, and
`action=status` reports what this chat's workers did — including which ran out
of rounds — so the answer never has to be reconstructed from log files.

The clamp only narrows to what the caller has, and for `tool_access: "all"`
that is *everything* the caller has — two read-only audit loadouts were stored
with ~200 tools each (shell, email, posting, the browser MCP surface) because
the model wrote "all". So the tool refuses an explicit "all", naming the
read-only set and the mutating tools it would have granted, and a loadout that
names no tool policy gets the read-only set the chat can grant (the harness's
own classification, the same one a research specialist gets) rather than
everything. `enabled_tools` may say `"@read_only"` for that set plus any other
tools by name. A model named at create/update is resolved then, with the
available ids in the error, rather than at `start` two rounds later. Tool
lists in the log and in `start`'s result are a preview and a count, never the
inventory.

A worker's run is filed under the worker's own chat, and the chat that started
it finds that run through the run record's `parent_session`. That link is what
the agent strip above the composer is built from: it polls
`/api/workbench/runs?session_id=<chat>` on its own cadence (and at once on any
delegated-work event), so it does not depend on the activity stream delivering
the launch event. That mattered: browsers allow six connections per host over
HTTP/1.1, and every open Odysseus tab used to hold two event streams for its
whole life, so with three tabs — or two and a streaming reply — every further
fetch queued indefinitely and the strip in the launching chat never asked. Hidden
tabs now drop their streams after 30 s and reconnect on return, the strip's
poll times out instead of wedging, and it says why in the console when it
cannot show what the server has (`[agent strip] …`); the runs route logs one
line per change so the same question can be answered from the server log. The
parent also receives a terminal `status` event for each worker, so the row
resolves where the user is looking instead of sitting at "running" forever.

The explicit-delegation gate covers built-in delegate tools,
`manage_agent_loadout`, and dynamically qualified MCP tools whose name ends in
`run_pi_task`. `manage_agent_worktree` is intentionally outside this launch gate:
it prepares or inspects a checkout but does not start delegated work. Publishing
continues to use its separate human-confirmation gate.

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

Queues are bound to `(session_id, run_id)` when accepted. Foreground preparation
and headless execution use stable steering identities separate from activity
telemetry. A different run must neither drain nor cancel that queue, and a later
turn must not adopt an unbound correction. Ending a run cancels its undelivered
messages rather than leaving them queued for a future task.

The chat's temporary Steering chip tracks that record's ID. Live/resumed
`steer_applied` events remove the pending badge immediately and promote the
instruction to a normal user message; marked-as-poll status requests reconcile
after a missed event. Cancellation/failure is surfaced honestly, and
the durable Control Room history remains available. Late network responses do
not insert a message into whichever chat the user opened next.

Persisted human messages retain `steer_id` for redraw deduplication. Identical
instructions sent twice have different IDs and remain two messages; peer events
never become human bubbles. Injection confirms delivery to the model, not that
the requested work succeeded.

A human steer also adds the tools its instruction names, within the turn's
existing policy restrictions, without rebuilding the system prompt. Runtime,
retrieval and peer-agent envelopes do not count as human turns or enter follow-up
intent retrieval. Tail guidance preserves earlier unfinished requests unless the
user changes or cancels them. `[agent-steer]` logs IDs, delivery state and added
tool names without copying the instruction text.

### Fleet lifecycle and navigation

The Control Room separates Overview, Activity and Steering, and opens loadouts
in a dedicated editor. The default compact fleet is paginated, with persistent
Open chat and Stop actions, a larger-card option, and per-agent steering drafts.
The existing docking, maximize and resize controls remain available. Workbench
run cards also expose Open chat and Stop without first opening the run log.

Archive removes an idle agent chat from the normal fleet without deleting its
transcript. The Archived view restores it, including after a server restart.
Archiving is refused while the chat or its descendants have live or queued
work. Completed child cards can be hidden and restored without removing their
activity history. These operations are owner-scoped; cleanup is not a stop
operation.

### Background learning

Memory and skill extraction use a turn-local conversation snapshot, not a
mutable session that may already contain a later request. Retrieval envelopes,
tool results and peer-agent messages are excluded before selecting the recent
conversation window. A chat with read-only or disabled memory cannot write
through post-response extraction. Simple documentation searches and other
lookup-only turns do not automatically become reusable procedure skills.

---

## 7. Reading the logs

A turn's behaviour is reconstructable from these lines.

| line | answers |
| --- | --- |
| `[agent-intent]` | what the turn was classified as, and which tools were selected |
| `[tool-routing]` | which source dominated, what was dropped and by which gate |
| `[agent-debug]` | how many schemas were sent vs selected, which admin schemas were intentional, and which MCP servers were demoted |
| `[agent-cache]` | whether the static prefix stayed stable and where request history first changed |
| `[agent-steer]` | which queued message reached which round, and what tools its human instruction added |
| `[context-profile]` | the profile, inline limit and trim target in force |
| `[agent-timing]` | prep breakdown, per-round elapsed, time to first token |
| `[agent-usage]` | input / cached / output tokens per round |
| `[agent] missing-tool self-unblock` | that recovery fired, with scope and which detector |
| `[tool-output] offloaded` | a result moved to the output store, with its recall id |

Two habits worth keeping when adding to them: report the **difference**, not a
truncated list (a list clipped at N answers "was my tool sent?" confidently and
wrongly), and state truncation inside the string when it happens — `_name_list`
exists for this.

## 8. Scoped research workflows

Every workflow carries a structured `record` (`agent_workflows.build_record`),
returned by `status`/`wait`, kept in the manifest once the run ends, and
headlined in the chat hand-off and the activity detail: the objective, the
selected agents with their bindings and outcome, the result summary with its
source (the synthesis agent, the single specialist of a one-agent run, or
nothing) and whether it is provisional, the specialists' raw outputs as
pointers and sizes apart from that summary, the handoff artifacts, changed
files (none: specialists are read-only), the checks the controller actually
ran (preflight, evidence that bound tools executed, a non-empty result) with
their outcome, and the unresolved issues — failed branches, timeouts, and the
`open_questions` a handoff itself declared. `verified` on the summary is always
false: none of those checks verifies the content of a claim. The record is also
the hand-off contract: it renders ahead of the synthesis text on both surfaces
the parent reads (the `wait`/`status` tool result and the next-turn hand-off
message), and unless `clean_record` holds — completed, nothing unresolved,
every check passed, not provisional — it ends with a reporting obligation that
names the failed branches and open questions the parent must state. A long
synthesis used to push the gaps below what the parent read first, and its
reply followed suit.

`orchestrate_agents` runs real specialist jobs through `launch_worker`, not a
second agent runtime. `start` accepts an objective, one to eight named specialists
with self-contained tasks, exact read-only tool bindings, optional selected skills
and models, plus an optional synthesis agent. It returns a workflow ID;
`status`/`wait` collect actual results and `cancel` stops the workflow's children.
Wait is bounded to 60 seconds. The overall deadline is 30–1800 seconds, and the
optional `retries` setting allows at most one read-only retry (default zero).
Follow-up actions may omit the ID when there is one unambiguous running workflow
in the same chat. Named loadouts are not workflow IDs; explicit IDs always use
the `workflow-...` value returned by `start`.

Research branches are required by default. Synthesis starts only after every
required branch has a completed, evidence-backed handoff. A caller may explicitly
set `allow_partial_synthesis=true`; that run remains partial and its synthesis is
instructed to label itself provisional and enumerate missing branches. Workflow
results separately report launched child runs, completed/incomplete/failed
research, usable handoffs, failed attempts, and synthesis status. A terminal
partial result uses exit code 2; a failed/cancelled/timed-out result uses exit
code 1, while launch/running and fully completed results use 0.

Workers remain normal chats and Workbench runs. Parent/child IDs, models, attached
tools, actual tool calls, attempts, handoff artifact IDs and synthesis status are
recorded. Child capacity comes from the parent policy; a limit of one queues the
stages instead of silently raising the limit. Launching an ad-hoc workflow never
writes a shared reusable loadout. Tool/model/skill/memory/private access is narrowed
to the parent's permissions. Research cannot post to integrations or edit skills.
Positive tool bindings remain authoritative with large or changing MCP catalogs.
Named profiles used with `send_to_session` require a fresh child (`session_id=new`);
ordinary messages to an existing chat keep that chat's own permissions.
Synthesis receives bounded, explicitly untrusted handoffs, and the parent gets one
durable report rather than one competing auto-continuation per child.
Detached workers refresh session-backed credentials before starting and promote
terminal SSE provider errors into failed worker outcomes. Those failures can use
the workflow's bounded retry instead of being mistaken for successful empty runs,
except for authentication and authorization failures: a second identical attempt
cannot fix an expired bearer, so those are recorded as not retried.

`start` builds a preflight row per agent before anything launches — model,
resolved tool bindings, per-server MCP connection state, credential state and the
expected handoff — and returns it as `preflight`, with `preflight_blocked` naming
the agents that cannot do the work they are about to be sent. Blockers are
positive findings only: an absent MCP manager reports nothing rather than
inventing an alarm. When every agent's credential is already known to be expired
or rejected, `start` refuses the whole workflow instead of manufacturing a run
whose only output is N identical 401s and a synthesis written over nothing.

Bindings must name a supported read-only tool. The rejection lists the entire
supported set and the `mcp__serverId__tool` form, so a corrected retry takes one
step rather than another guess, and an unusable MCP binding says why it is
unusable — server disconnected, tool disabled, not read-only, or unknown — with
the connected servers named.

Loading `manage_skills` returns `loaded_not_run`; relevant research procedures
include an executable capability preflight. Batch-loaded skills now promote all
declared tool dependencies, including supported toolset aliases, subject to policy.
Only actual launch receipts establish execution. Explicit research-orchestration
requests with no launcher call get one bounded nudge, then a truthful non-execution
answer. Running/partial workflows cannot be presented as completed research.
Ordinary questions, negated delegation requests and untrusted worker/skill text
do not authorize launching agents, and authorization is a property of the CHAT
rather than of its latest sentence: a human request grants it for that chat
(`delegation_granted` in the chat's settings), an explicit human prohibition
takes it away, and ordinary follow-ups in between are not re-interrogated. Only
a human message moves it — the recogniser reads `_delegation_intent_text`, which
already excludes worker output and injected role=user envelopes. Re-deriving it
every turn refused users who had asked two messages earlier, and the model's
response to being refused was to POST `/api/agents/launch` through the generic
API bridge, bypassing the gate completely; that route is now refused by
`app_api` and points at the real tools. A human `continue` can resume an
existing authorized workflow without starting it again. Negation and question forms are
scoped to their own clause: a report of what failed ("no agents ran") no longer
cancels a request made in the same message ("relaunch the two specialists"), and
restart/relaunch/retry phrasings count as orchestration requests.

A skill's `requires_toolsets` entry resolves as an exact tool name, a connected
MCP server (by id or display name, expanding to its tools), or a known prose
alias. Only entries that name nothing real are reported as bad front matter;
an entry that resolves but is switched off for the turn is policy working as
configured, not metadata to fix. `manage_skills action=add` says so at authoring
time. Domains starved by a deliberately narrowed loadout are recorded as expected
observations rather than warnings.
Stop/status/wait requests do not authorize new launches. Research branches with
bound web or MCP tools must make successful calls to those bindings before the
controller can mark their work complete. A missing permission-store read blocks
execution instead of granting default access.

Cancellation releases worker capacity even before a worker coroutine begins.
Completed artifacts remain retrievable from their owner-checked child chats when
the bounded activity registry rotates. Restarted workflows are marked interrupted
and are not automatically replayed.

Deep Research remains a separate single-job workflow. A corrupt/empty final
synthesis gets one lower-temperature recovery attempt using the same model and
collected evidence, within a shared 180-second budget. Unrecovered work retains
its evidence and is labeled partial. The API's terminal `status=done` still means
results are available; inspect `outcome` and `synthesis` for completeness.

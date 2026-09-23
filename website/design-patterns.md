# Design patterns and conventions

Recurring decisions in this codebase, and the reasoning behind them. Most were
paid for by a real failure — where that is the case, the failure is named,
because a rule without its reason gets dropped by the next person who finds it
inconvenient.

This is a guide for writing code that fits, not a style guide. Formatting
conventions live in [`CONTRIBUTING.md`](../CONTRIBUTING.md).

---

## Policy fails closed. Optimisation fails open.

Two different defaults, and mixing them up is how both kinds of bug happen.

A **policy** decision — may this agent use this tool, read this file, act for
this user — defaults to *no* when it cannot be evaluated.
`owner_is_admin_or_single_user` returns `False` if it cannot resolve the auth
manager. A malformed vault sensitivity label is treated as private, not public
(`docs/vault-retrieval.md`).

An **optimisation** — is this call a duplicate, is this dependency down, is this
schema worth sending — defaults to *carry on* when it cannot be evaluated.
`circuit_breaker.is_dependency_down` treats anything it does not recognise as a
request-level fault, so it will not trip: a breaker that fails open is a missed
optimisation, one that fails closed on a misread exception takes a working
integration offline.

Write down which kind you are building, in a comment, at the point of the
default.

## The offer and the enforcement must agree

If execution will refuse a capability, do not advertise it. A tool that appears
in the schema list and then returns "requires an admin user" costs schema tokens
every round, and — worse — the model picks it, gets refused, and spends rounds
rediscovering that.

This has been fixed three times in different places: the non-admin blocklist was
enforced only at execution and never subtracted from the schema list; a demoted
MCP server was listed in the prompt as callable while its schemas were gated;
the built-in MCP catalogs had the same gap. The rule is in the behaviour spec:
unavailable capabilities "must not fail later as phantom tools"
([`specs/personal-directories-and-tool-routing.md`](../specs/personal-directories-and-tool-routing.md),
invariant 6).

When a capability genuinely is present but not attached right now, say so and
say how to get it — do not silently omit it, or the model will report it
missing to the user.

## Clamp and report, do not reject

When a caller asks for more than it may have, narrow the request and tell them
what was narrowed — rather than failing the whole thing or silently granting it.

`src/agent_loadouts.py` is the clearest case: an agent authoring a worker
loadout gets every capability intersected with its own chat's policy, and the
result carries a `narrowed` list naming each reduction. The agent can then
decide whether the clamped loadout is still worth starting.

The same shape appears in the MCP always-bound budget (demote the largest
server, report which in `[agent-debug]`) and the missing-tool recovery (widen to
the evidenced domains, name them in the log).

## Terminal versus transient

Before retrying anything external, decide which kind of failure it was.
Retrying a dead credential forever while reporting success is the same bug as
telling a user to reconnect a working account because the network blipped.

`src/oauth_errors.py` is the canonical split: the OAuth error *code* decides
(RFC 6749 §5.2), status is the fallback when the body is unreadable, and no
response at all is always transient. `src/circuit_breaker.py` applies the same
distinction to dependencies — a 502 is an outage, a 400 is our bug.

A terminal failure should stop being retried and become visible. A transient one
should back off and stay quiet.

## One definition, imported

The calendar sync and the mail transport each grew their own copy of the OAuth
failure classifier. They had diverged before either shipped: the same
`400 invalid_request` told calendar users to retry and mail users to
re-authorize a working account.

Two copies of a rule are two rules. When a second subsystem needs the same
logic, move it somewhere neutral that both can import — not into whichever
module happened to write it first, which inverts the dependency and is why the
duplication looked reasonable at the time.

The agent tool allowlist had grown *three* copies of the same inversion —
`agent_profiles.session_patch`, `task_scheduler`, and `saveAgentConfig` in the
browser — each subtracting the allowed set from a different registry, and none
of those registries holding an MCP name. They now share
`src/tool_policy.py::allowlist_permits`, which also fixed the second half of the
bug: the rule is applied where the decision is made, not baked into a stored
denylist that a later-added tool is simply missing from.

## Recognise by shape when names are not yours to enumerate

An allowlist works when you own every name. MCP tool names carry a per-server
hash (`mcp__77d1a280__firecrawl_agent_status`) and multiplexed tools carry the
verb in their arguments (`delegate_to_claude_code {"action": "poll"}`). A static
list can hold neither.

The duplicate-call guard had to learn this twice: first that polling must be
recognised by name shape, then that it must also be read out of the arguments.
Both times the symptom was identical — a job that could never be observed
finishing, because its second poll was answered from a memo.

If you are adding an exemption list for tools, ask what happens when the name is
generated at runtime.

## Conservative default for the unclassifiable

When a classifier meets something it does not recognise, the unknown case goes
in the *restrictive* bucket, and a comment says which way "wrong" costs less.

`_lane_for_task` puts a new action, a typo, a plugin's action or an unreadable
row in the cap-1 model lane: wrong in that direction costs queue time, wrong the
other way runs two LLM jobs on a box sized for one. The ledger skips anything it
cannot prove safe to collapse, so every failure mode is "kept more than needed"
rather than "a path is gone".

## Owner scoping at the boundary

Every route that touches user data resolves the caller and filters by it —
`src/auth_helpers.py::owner_filter` for queries, `effective_user` for the
identity. Two details that have bitten:

- An exact owner match. When auth is on, a null-owner row is *not* the caller's;
  treating it as shared let an authenticated agent reach sessions the listing
  tools hid.
- The HTTP route is not the only entry. A tool called by an agent reaches the
  same code without passing the route's check, so the check belongs in the
  shared layer — `agent_control.launch_worker` needed its own parent-session
  owner check because only the HTTP route had one.

## Batch anything that edits a cached prefix

Providers cache request prefixes. Editing history in the middle moves the cache
boundary, so an edit *every round* invalidates the cache every round and costs
more than it saves.

Both mechanisms that rewrite history — the replayed-reasoning prune and the
execution ledger — accumulate a window plus slack and then edit once, and both
are append-only so the invalidation point moves toward the tail rather than back
to the front. Mid-turn directives go in as a tail **user** message, never
`role: system`, because `llm_core` hoists system messages into the instructions
block at the front. See [`specs/prompt-prefix-stability.md`](../specs/prompt-prefix-stability.md).

## Untrusted content is data, never instructions

Documents, memories, notes, web pages, tool output, skill text and MCP
descriptions are all user- or network-controlled. They are fenced with an
explicit warning before entering a prompt, and anything that rewrites such a
message reproduces the fence byte for byte. See
[`THREAT_MODEL.md`](../THREAT_MODEL.md).

The corollary for code: never build a security decision out of text that came
from one of these surfaces.

## Persist atomically

`core/atomic_io.py` (`atomic_write_json`, `atomic_write_text`) writes to a temp
file, fsyncs, and renames. Every JSON store under `data/` should go through it —
a half-written settings or prefs file is a broken install, and the app writes
these while the user is using it.

The related rule for multi-user stores: read-modify-write the whole document,
not the slice you care about. `routes/prefs_routes.py` documents the case where
writing a flat payload over a `_users` map destroyed every other user's
preferences.

## Degrade honestly, and say so

An unavailable subsystem reports itself unavailable with a reason and a setup
action. `src/capabilities.py` is the registry; `unavailable_tools()` is what
keeps them out of the schema list; `src/service_health.py` is what the
diagnostics page reads.

Health checks must resolve credentials the same way the real call does. The
provider probe read `ModelEndpoint.api_key` straight off the row, which is empty
by design for subscription providers, and reported a provider as `no_models`
while it was serving every request of the session.

## Settings belong in the schema

User-selectable behaviour goes in the settings store with a spec in
`src/settings_schema.py`, which gives it a typed UI control and a default. Keys
following the existing naming conventions are auto-registered, so
`missing_specs()` stays empty — check it after adding a setting.

Environment variables are for bootstrapping: host paths, ports, mounts,
secrets supplied by an orchestrator, deployment-level gates. The distinction is
stated in
[`specs/personal-directories-and-tool-routing.md`](../specs/personal-directories-and-tool-routing.md).

A tuning constant that an operator might reasonably need to change should be a
setting with the constant as its default — not a literal buried in a function.

## One timeline

`src/agent_activity.py` is the single feed every kind of work reports to: chat
turns, sub-agents, delegated coding jobs, background shell jobs, worktree
publishing, steering transitions. It persists as per-session JSONL, streams over
SSE, and is owner-scoped by the routes.

When you need history for something new, publish to the feed rather than adding
a store. A second store has to re-earn persistence, streaming, scoping and
pruning, and then be kept consistent with the feed rendered next to it.

## Logs are read by someone debugging at 2am

Three habits, each from a line that answered a question wrongly:

- **Report the difference, not a truncated list.** `[agent-debug]` logs
  `schema_without_selection` rather than the first 15 names, because a clipped
  list made a missing tool look identical to the healthy case.
- **State truncation inside the string.** `re-armed 33 tool(s) [...25 names...]`
  is a lie by omission; `_name_list` renders `… +8 more`.
- **Log the reason, not just the outcome.** A tool dropped between retrieval and
  selection now reports which gate did it.

## Tests

`tests/TESTING_STANDARD.md` is the standard. Two things worth repeating here:

- **Behaviour first.** Assert on what the code does, not on its source text.
  Source assertions are a last resort for things with no reachable seam — and
  they rot: several of this suite's stale tests are source assertions that
  outlived the code they described.
- **Extend the existing file.** The suite has over 850 test files and a taxonomy
  (`tests/_taxonomy.py`). A new file for a behaviour that already has one splits
  the coverage and both halves drift.

When changing code with a subtle invariant, write the test that fails first and
keep it in the commit. Several guards in the agent loop exist because a test
proved the guard was load-bearing.

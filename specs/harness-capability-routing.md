# Harness capability routing refactor

Status: implemented, 2026-09-17; live-provider acceptance pending.

## Review and objective

Request understanding currently runs in chat promotion, domain classification,
semantic retrieval, keyword fallback, and several late tool-set rewrites. A
selection miss can look like a missing capability. Refusal-text rearming happens
too late and does not update fenced-tool models. This pass makes inference
advisory and discovery explicit while preserving execution-time authorization.

## Scope and sequence

1. Extract dependency-light, typed intent assessment and human-message provenance
   into one shared module. Preserve compatibility helpers and explicit chat-mode
   behavior. Unknown substantive requests must not be classified as greetings.
2. Introduce a deterministic tool-selection plan with candidate provenance,
   explicit constraints, stable bounded selection, and deferred permitted tools.
   Heuristic relevance exclusions are not permission denials. Explicit requested
   tools and caller bindings take precedence over similarity suggestions.
3. Add provider-neutral `discover_tools`: search the permitted, already-connected
   catalog and load a small matching subset for this turn. No connections,
   settings changes, external actions, or permission grants occur in discovery.
   Both native-function and fenced-tool models receive newly loaded definitions.
4. Exercise the actual loop and dispatcher as well as pure functions. Cover
   paraphrases, changed subjects, continuations, synthetic context, permissions,
   unavailable embeddings, large catalogs, cancellation and concurrent turns.
5. Document results, remaining live-provider acceptance, and operational traces.

## Invariants

- Intent assessment describes a request; it cannot grant access.
- Existing user/global disables, positive profile allowlists, ownership, private
  data, MCP-server restrictions, plan/read-only restrictions, delegation policy,
  and human approval remain authoritative. Execution rechecks current policy.
- Unselected and forbidden are distinct. Discovery cannot reveal forbidden tools
  or promote them through aliases, stale results, or a late-connected server.
- Tool discovery state belongs to one run, not a module-global mutable registry.
- Semantic retrieval is optional and bounded; exact-name and lexical discovery
  work without embeddings. Unknown tools are never invented or executed.
- Selection traces contain decisions and names, not credentials, prompts,
  private document content, or raw tool outputs.
- Canonical tool schemas are not mutated. A discovery result is a catalog result,
  never proof that the requested action was executed.
- Token efficiency is an acceptance requirement: bounded eager schemas and
  discovery results, stable ordering/prompt prefixes, compact tool metadata, and
  no extra model-classifier request per turn. Record schema-token estimates and
  discovery overhead, not only counts of passing tests. An explicit broad
  loadout can exceed the advisory budget; never silently remove its bindings.

## Research informing the design

Official OpenAI documentation describes deferred tool definitions and
application-controlled tool search for state-dependent catalogs. This project
will implement that pattern using ordinary function/fenced tools, not require a
particular API, model, or hosted agent service. Discovery adds a step, so common
tools remain eagerly available and comparisons must include latency and tokens.

- [Tool search](https://developers.openai.com/api/docs/guides/tools-tool-search)
- [Trace grading](https://developers.openai.com/api/docs/guides/trace-grading)
- [Agent workflow evaluation](https://developers.openai.com/api/docs/guides/agent-evals)

## Explicit non-goals

No provider/model migration, new external infrastructure, autonomous permission
expansion, full durable-run-state rewrite, or deployment/push in this pass.
No claim of universally high task success based only on mocked model streams.

## Acceptance

- [x] Shared assessment preserves current supported requests and ignores
  tool/peer/retrieval text as human intent.
- [x] A missed permitted tool can be discovered and used in the next round.
- [x] Discovery is bounded and useful when embeddings are unavailable.
- [x] Denied/unknown/private/admin-only tools cannot bypass policy through search.
- [x] Selection is stable, explainable and isolated across concurrent turns.
- [x] Native and fenced loop integration tests pass without live providers.
- [x] Targeted routing, privacy, workflow, approval and stream regressions pass.
- [x] Record deterministic evaluation results and a live deployment checklist.
- [x] Verify schema-token bounds, deterministic ordering and no per-turn
  classifier model call; report any discovery extra-round tradeoff honestly.

## Implemented arrangement

`intent_assessment` owns human provenance, continuation and low-signal assessment.
Existing domain vocabulary remains as advisory candidate generation, not a
permission authority. `tool_selection` produces one stable initial plan from
those candidates, caller/profile bindings, explicit names and skills.
`tool_discovery` searches a turn-local copy of the permitted canonical catalog;
`tool_execution` remains the final action/ownership/privacy/approval boundary.

Initial advisory limits are 24 tools / 3,000 estimated schema tokens. Initial
attachments and all later automatic additions share 32 tools / 5,000 estimated
schema tokens. Explicit caller/profile/named-tool/skill bindings can exceed the
advisory initial limit and this is recorded as `explicit_budget_override`.
Discovery returns at most eight new tools per call, skips attached definitions,
and reports budget exhaustion separately from missing capabilities. Exact-name
lookups need no embedding request. Semantic fallback has a two-second timeout.

Native models receive compact definitions only through the tools field; fenced
models receive newly attached definitions once in a reference-data envelope.
No full MCP catalog is duplicated into the native prompt. New schemas are
accounted for before subsequent context trimming. Global and session disables
are freshly checked, including alias forms, before dispatch; unreadable policy
fails closed. Grants never expand implicitly within an active turn.

## Token measurement and limits

An offline diagnostic candidate fixture on this checkout selected eight compact
definitions costing approximately 782 tokens, versus 9,796 tokens for all 83
built-in definitions. This compares schema payloads, not an old-vs-new live bill:
the prior harness already selected subsets on many turns. Actual savings depend
on the request, provider tokenizer, cache behavior and discovery extra rounds.
Metrics include initial schema tokens, deferred count, discovery calls, discovered
count, discovery schema tokens and explicit-budget overrides.

See [acceptance and rollout checks](../docs/harness-routing-acceptance.md). The
30-case corpus supplies regression inputs and labels; offline replay verifies
provenance/permission/intent properties, not full semantic task-success scores.

## Validation record

On Windows / Python 3.14, the combined 46-file regression selection finished
with **928 passed, 2 deselected**. Compilation and `git diff --check` passed.
The two exclusions are existing `tests/test_agent_turn_cost.py` cases
`test_the_custom_lane_never_builds_the_fastembed_fallback` and
`test_a_healthy_http_lane_is_still_returned`: both reference
`embedding_lanes._build_custom_client`, absent on unchanged HEAD. They failed
in the initial broad run and were not rewritten to mask the baseline API drift.
An existing collection-time registry eviction in `test_unknown_tool_calls.py`
was removed so private-grant handler tests remain isolated in a combined run.

This is not the entire repository suite or a live-provider deployment test.
Provider instruction-following, production token bills, and an eight-worker
live run remain acceptance work after deployment. No deployment performed.

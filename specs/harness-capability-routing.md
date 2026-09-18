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

### September 17 corrective-turn hardening

Short method-only feedback and backward references use recent human task context,
not a semantic query for the correction itself. A new actionable clause remains
a new request. MCP use does not itself imply MCP administration. Connectivity
alone supplies discovery inventory, never an eager schema attachment; explicit
loadouts, relevant matches and recent use retain their existing priority.

Automatic personal-document retrieval is skipped for bounded low-information
feedback; explicit RAG opt-in and substantive grounding requests remain intact.
The active editor was already excluded from the agent prompt when irrelevant;
the route now avoids unnecessary lookups for feedback turns as well.

Private-read checks share a pure policy between discovery and dispatch. Fresh
session settings can narrow the request's grant, never enlarge it. Unrestricted
shell/Python and the existing unconfined MCP file wrappers remain gated; public
file tools remain available subject to their path policy. Denials carry
`private_vault_grant_required`, distinguish policy from a missing workspace,
and state that enabling private reads grants private-vault access, not just Git.

See [log evidence and remaining validation](../docs/harness-log-review-2026-09-17.md).

### September 17 local repository sync

The 07:02 deployment run confirmed successful AI Mind writes but exposed a
capability gap: GitHub metadata tools cannot perform a local `git pull`, and
unrestricted shell access correctly remained gated. `manage_git` now exposes
typed inspection, staging, commits, branches/tags, switching, fetch/pull,
upstream configuration and confirmed push/fast-forward merge/branch deletion.
Legacy `manage_agent_worktree` repo-list/status/pull aliases remain. Explicit
local Git requests bind the general capability before
semantic retrieval, still intersected with the same policy ceiling/budget.
The task note distinguishes local synchronization from GitHub Actions and
does not implicitly authorize an operation merely by selecting its schema.

A bare repository URL/slug/path can ground the immediately unresolved human
Git task; unrelated/completed tasks and untrusted runtime messages cannot.
Pure reads and model suggestions never authorize an automatic pull.
The service uses only approved checkouts and configured GitHub remotes, requires
clean fast-forward checkouts, and excludes vault roots and unsupported
execution-inducing configuration. GitHub credentials obey the live integration
ceiling; writes additionally require deployment write opt-in. Push/merge/deletion
always use exact-call single-use confirmation and revision checks, independent
of automatic approval mode. Routine changes remain direct unless ask_all applies.
History rewrites, arbitrary commands and conflict-resolving merges are not exposed. This deliberately
does not widen unrestricted Bash/Python access. See
[supported scope and deployment checks](../docs/agent-worktree.md).

Dirty-checkout updates use a single typed `pull_with_restore` operation rather
than shell access or a general stash-pop primitive. It bounds saved content,
keeps untracked files in place, rejects any upstream overlap before checkout,
fast-forwards only, and restores only the paths that were locally changed. A
durable `refs/stash` entry remains available if restoration fails or the process
stops mid-operation. Availability of this action does not turn a diagnostic or
read-only request into authorization to update the checkout.

The 14:14 deployment exposed a native argument-contract regression: models filled
unused multi-action properties with empty values, and the Git handler rejected
the call before Git ran. Git/legacy repo-action boundaries now discard only known,
irrelevant neutral placeholders and use documented defaults for empty optional
fields. Unknown fields and meaningful disallowed overrides remain invalid;
required fields are never supplied or repaired. Single-use Git/repo-action approval
fingerprints use the identical normalization without weakening target/revision binding.
Git schemas preserve compact field guidance and explicitly opt out of implicit
Responses strict normalization; the adapter preserves explicit boolean `strict` settings.
Distinctive Git CLI HTTPS credential diagnostics also select the scoped Git tool
through the existing permission ceiling instead of relying solely on web retrieval.
They do not grant execution authority or shell credentials.

### Earlier offline measurement (baseline)

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

## Extension boundaries

Per-agent `agent_instructions` are persisted session snapshots and appended
outside the shared base-prompt cache, subordinate to platform policy. Reusable
profile edits do not mutate existing agents. Declarative plugins group existing
skill/tool/MCP/model references through owner-scoped explicit selection, never
execute installers or grant new private-data rights. PromptScript is an
operator-side compiler, not a replacement runtime policy engine. Remote skill
imports are reviewed snapshots and remain drafts until explicit publication;
they cannot enter automatic retrieval while unreviewed. See the
[extension workflow and limitations](../docs/agent-extensions.md).

## Validation record

For bounded dirty-checkout synchronization, **352 tests passed, 6
platform-specific tests skipped** across real temporary-repository save/restore,
fast-forward, overlap refusal, provider argument conversion, approvals and
routing. The integration cases verify that unrelated upstream files survive,
staged/unstaged/deleted paths are restored, untracked files remain, and unsafe
attribute drivers refuse before stashing. Network transport is mocked; the
deployed dog-trainer checkout remains a post-rebuild acceptance test.

For the 14:14 native Git argument follow-up, **342 tests passed, 6 platform-specific
tests skipped** across Git operations/security, native provider conversion,
schema compaction, approvals and routing. Expanded log/commit calls run against
temporary real repositories; network transport remains mocked. These tests do
not claim a successful pull in the deployed container; rebuild and live acceptance
are still required.

For the scoped Git expansion, **621 tests passed, 6 platform-specific tests
skipped** across repository operations, approval/routing, worktree publishing,
privacy and tool-policy suites. Three existing Windows/environment tests were
excluded from that green run and independently reproduced on clean unchanged
HEAD `fd0e238`: worktree-policy temp-root allowance, private-key mode diagnostic,
and MAC-key POSIX mode assertion. No production checkout or network write was
used by the Git tests; remote transport is mocked around real temporary Git
repositories. Docker rebuild and live provider/deployment acceptance remain
operator checks, not claimed test coverage.

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

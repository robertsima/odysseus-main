# Harness routing acceptance

The deterministic suite in `tests/test_harness_routing_integration.py` is the
release gate for capability discovery. It uses mocked model streams and no live
provider or connector, but retains the real loop, discovery catalog filtering,
schema construction, parsing, and discovery dispatch.

## Automated acceptance

- A permitted deferred tool missed by initial selection can be found by
  `discover_tools(query, max_results)` in round 1, attached from its canonical
  schema in round 2, and then executed.
- Native-function and fenced-tool models receive the newly loaded definition.
  The fenced prompt contains a usable signature, not merely a tool name.
- Discovery is bounded by `max_results` per call and by its own per-turn
  allowance (32 tools / 4,096 schema-token estimates by default). The allowance
  counts only what discovery loads: schemas the round already sends are
  reported as already attached, never loaded twice, and never spend it. Initial
  schemas have stable ordering.
- Disabled, private, admin-only, unknown, and connector-restricted names are
  neither returned nor attached. Discovery output cannot grant permission or
  activate an arbitrary MCP server.
- Loaded names are turn-local. Concurrent turns cannot observe or execute one
  another's discovered capabilities.
- Discovery does not issue a separate model/classifier request. The mocked
  provider sees only ordinary agent rounds.
- Canonical schemas are not mutated by compaction or attachment.

The JSON corpus in `tests/fixtures/harness_routing_cases.json` supplies
paraphrases, changed subjects, continuations, untrusted synthetic context,
explicit constraints, connector-name collisions, and caller bindings for the
batch planner/intent evaluation. Expected tools are always a subset of that
case's explicit `permitted_tools`.

The corpus labels are not a claim of 30 successful live tasks. Offline replay
checks provenance, substantive-vs-casual assessment, exact names and permission
ceilings. Mocked multi-round tests separately verify native and dynamic MCP
fenced calls, concurrent live-loop isolation, immutable caller inputs, context
reserve growth and mid-turn revocation.

Default schema bounds, all advisory:

- The initial selection is trimmed toward `agent_tool_budget` (40 tools) by
  dropping domain-seeded tools. Retrieved, forced and other protected tools
  always stay, so it can exceed 40. It has no schema-token cap.
- A chat's sticky tool set restarts from the turn's own selection once it would
  pass 48 tools.
- Small connected MCP servers stay bound up to 8 tools per server and 24 in
  total; a larger server is demoted to retrieval.
- Discovery adds at most 32 tools / 4,096 schema-token estimates per turn, on
  top of whatever the round already sends.

Explicit bindings may exceed these bounds and are reported. Discovery metadata,
tool outputs, reasoning and conversation text are additional costs; do not
confuse a schema bound with a total-request bound.

## Live-provider checklist

Local regression record (2026-09-17): 928 passed across 46 selected files;
two documented pre-existing embedding-test failures were excluded. See the
[validation record](../specs/harness-capability-routing.md#validation-record).

Mocked streams cannot establish provider instruction-following or production
latency. Before rollout, exercise one native-function model and one fenced-tool
model against a non-production permitted connector and record:

1. initial and post-discovery schema count/token estimates;
2. selection and discovery names/provenance, with no arguments or tool output;
3. number and latency of model rounds;
4. the discovery result count and budget-limited status (redact query text);
5. execution-time policy recheck outcome;
6. behavior after revoking permission between discovery and execution; and
7. concurrent runs with distinct permitted catalogs.

Do not log credentials, private content, raw prompts, raw discovery documents,
or raw tool results. A discovery result proves only catalog lookup; it is not
evidence that the requested action executed.

# Tool schema and usage-ledger efficiency

## Evidence
Recent agent runs showed 38–81% of prompt tokens attributable to tool schemas or fixed overhead. Existing per-message metrics report token counts, but cost is recalculated in browser local storage and cannot support durable comparison. Privacy-sensitive Vault and Journal content must never enter usage records.

## Changes
1. Generate compact native schemas for API models: preserve tool name, parameter names/types/enums/required fields and a short function purpose, but remove verbose parameter prose. Full schemas remain the canonical execution/validation source.
2. Continue retrieval-first tool selection, and expose selected schema count/tokens plus compaction savings in per-turn metrics.
3. Persist a content-free per-turn usage ledger scoped by session and owner. Store token/caching/schema/round/tool-count measurements and optional modeled cost only when a trusted server-side price is available; do not store prompts, tool args/results, RAG excerpts, document paths, journal labels, or memory text.
4. Show a compact footer signal for cache reuse and schema overhead; keep detailed counts in the existing stats popover.

## Acceptance criteria
- Compact schemas preserve callable names, JSON parameter shape, required fields, and enums.
- Compact selected schemas cost less than canonical schemas and tool execution remains unchanged.
- Every non-incognito saved assistant turn creates one content-free ledger row; incognito turns create none.
- Ledger records are owner/session scoped through foreign-key data, contain no free-text user content, and existing session totals continue working.
- Metrics/UI expose cache and tool-schema signals without retrieved or private content.

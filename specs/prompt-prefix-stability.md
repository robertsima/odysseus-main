# Prompt-prefix stability on the Responses path — 2026-09-10

## Evidence

A 28-round coding turn on the ChatGPT-subscription Responses endpoint logged
`[agent-usage] input≈40k cached=16896 cache_write=0` on every round while the
prompt grew from 50k to 56k tokens. The cached amount never moved: only the
static prefix (instructions + tool schemas) was served from cache and the
whole conversation (30–45k tokens) was re-uploaded uncached each round. In an
ordinary chat every single-round turn logged `cached=0`.

Code causes, in order of weight:

1. `_append_tool_results` popped `reasoning_items` off the oldest assistant
   turn on every round to keep a 3-round replay window. Those items are
   emitted ahead of the turn they belong to, so the edit lands in the middle
   of the already-cached input and moves the cache boundary every round.
2. `llm_core` hoists every `role: system` message into the single
   instructions block. The loop-breaker, self-unblock, verifier, nudge and
   memory-answer paths appended `system` messages mid-turn, rewriting the
   front of the prefix on exactly the longest rounds.
3. The Responses payload carried no `prompt_cache_key`, so consecutive rounds
   of one conversation could be routed to different cache shards.
4. Between turns the tool set is rebuilt by retrieval (no similarity cutoff)
   and the system prompt is derived from it, so the prefix differs turn to
   turn; single-round turns under ~1k tokens are never cache-eligible anyway.

## Priorities

1. Prune replayed reasoning in batches: let the window overrun by a slack
   equal to the window (minimum 4), then cut back to the window in one edit.
   The prefix is invalidated once per ~window rounds instead of every round.
2. Deliver mid-turn runtime directives as a labelled user-role message at the
   tail (`_harness_directive`) instead of `role: system`, so the instructions
   block stays byte-identical for the whole turn.
3. Send `prompt_cache_key = <Odysseus session id>` on Responses requests
   (setting `chatgpt_prompt_cache_key`, default on, in case a proxy rejects
   the field).
4. Cross-turn stability is addressed by the tool-schema hygiene spec (fewer,
   more deterministic tools per turn); it is a smaller win than 1–3.
5. Emit privacy-safe per-round cache diagnostics: a hash of the static
   system/tool-schema prefix and a request-history continuity flag. When an
   older message changes, log only its index and short before/after hashes—
   never prompt or tool-output content. This distinguishes a legitimate tail
   append from the mid-history mutation that invalidates a long cached prefix.

## Expected effect

On a multi-round agentic turn the cached share of input should climb round
over round instead of staying at the prefix: roughly 60–75% of input tokens
served from cache from round 3 onward (versus ~35% flat before), and a
shorter time to first event on late rounds. Verify with the existing
`[agent-usage] cached=` log line: it should increase with each round and only
drop at the batch prune.

## Acceptance criteria

- `tests/test_responses_reasoning_replay.py`: nothing is popped inside
  window + slack rounds; one round past it, exactly `window` turns keep their
  reasoning items.
- No `messages.append({"role": "system", …})` remains in `stream_agent_loop`;
  the five runtime notes go through `_harness_directive`.
- `_build_chatgpt_responses_payload(..., cache_key=sid)` emits
  `prompt_cache_key`; the setting turns it off.
- Cache diagnostics remain stable when messages are appended at the tail and
  report the first changed index when an earlier message is replaced or
  removed. Diagnostic logs contain hashes and counts only.
- Existing reasoning-replay, Responses-tools and agent-loop tests pass.

## Follow-up — 2026-09-27 bundle (Codex backend, gpt-6-luna admin chat + 15 workers)

Joined `[prompt-prefix]` to `[agent-usage]` per session: 6.10M input tokens,
1.67M uncached (72.7% cached). Uncached tokens beyond normal tail growth, by
cause:

| cause | events | excess uncached |
| --- | --- | --- |
| tool list / instructions changed at a turn start (sticky set grew 2-4 tools every turn, restarted at the 48 cap) | 8 | ~307k |
| tools attached mid-turn by `discover_tools` | 4 | ~178k |
| execution-ledger batches (2 went back to the turn start for resolved deferred failures; 4 on a turn's last round) | 9 | ~165k |
| worker rounds at 0% with nothing changed (backend eviction/routing; 28-238 s gaps) | ~9 | ~90k |
| worker cold starts | 16 | ~85k |
| turn start with only `history_shrank`, yet only instructions+tools cached | 2 | ~65k |

Every tools-only change cached 0 and the one instructions-only change kept
~4.6k (the 64 tool schemas), so the Codex backend's prefix is tools, then
instructions, then `input`. No soft-trim or compaction ran (400k window,
200k budget): `history_shrank` here is the previous turn's tool items not being
persisted, not a sliding trim.

Changes: the ledger waits for context pressure (60% of the route's input
budget) and no longer rewrites a deferred failure behind its own stretch unless
the prompt is past 85%; `[agent-usage]` carries `session=`; `[prompt-prefix]`
carries `first_diff_item`/`prev_items`/`diff_kind`, `instr_diff_at` and
`tools_added`/`tools_removed`, so the next bundle can pin the two unexplained
turn-start misses to an input index.

Tool-set churn (the first two rows): on hosted API routes with a 128k+ window
the sticky cap is 96/128 instead of 48, the set grows by whole domain chunks,
a restart keeps the chat's most-used tools and the turn's domains, a
`discover_tools` load brings its domain siblings, and tools that join later
are appended to the end of the tools array (`_sticky_tool_cap`,
`_sticky_tool_selection`, `_sticky_order_schemas`). A 30-turn, multi-domain
simulation (`tests/test_tool_set_stability.py`) went from 453 to 205
tool-set changes over 20 chats, and its re-billed-token proxy from ~2.3M to
~0.8M per chat. Codex still misses on any tools change; the remaining
changes are mostly tools no domain covers, picked for the first time.

## Follow-up — 2026-09-29: keep the tools array fixed (`allowed_tools`)

The 3-day bundle to 2026-09-29: 113M input tokens, 81% cached, 21.2M uncached.
The first round of a turn was 6% cached (9.9M of the uncached). Requests whose
tool list changed cached 16% (additions), 4% (additions plus removals), 0-4%
(removals, reorders, a tool-free round) — ~7.3M uncached in all; nothing
changed, 94%. The admin chat's list grew 46 → 132 over a day, and the system
prompt changed with it (its tool list and domain rules follow the tools).

OpenAI documents the fix for a harness that must vary what may be called: keep
`tools` identical and narrow with `tool_choice: {"type": "allowed_tools",
"mode": "auto", "tools": [...]}`; a tool-free request sends `tool_choice:
"none"` instead of dropping the tools. `tool_choice` is not in the list of
settings that change the prefix. Measured on gpt-6-luna by another pi-based
harness (senpi PR #2112): removing a tool cached 0 of the next turn; the
`allowed_tools` form cached 19.5k.

Changes (`src/stable_tools.py`, ChatGPT/Codex route, GPT-5.6 and later,
setting `chatgpt_stable_tools`):

- a chat's tools are *declared* in first-offered order, persisted under
  `<data>/prompt_cache/declared_tools`, and sent on every request; the
  round's selection goes in `allowed_tools`, a tool-free round sends `none`;
- a tool a round withholds (a worker follow-up's launchers, a sticky-set
  restart) stays declared, so narrowing no longer rewrites the prefix;
  a new tool is appended (one miss, as before); past 160 the list starts over;
- the system prompt's tool-dependent parts are built from the declared set, so
  they change only on the request where the tools change; a note beside the
  request names the callable tools;
- a backend that rejects `tool_choice` is remembered (`_rejected_param_retry_chunk`)
  and then gets only the callable tools, as before;
- `[prompt-prefix]` shows `callable=N`.

Not done: GPT-5.6+'s `additional_tools` input item (append a new tool at the
end of `input` instead of changing `tools`) would also make growth free, but
its position has to survive history collapse and trimming.

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

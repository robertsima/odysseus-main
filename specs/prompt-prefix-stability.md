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
- Existing reasoning-replay, Responses-tools and agent-loop tests pass.

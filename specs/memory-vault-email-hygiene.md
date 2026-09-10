# Memory, vault and email hygiene — 2026-09-10

## Evidence

After a short brainstorming chat about startup names:

- `Auto-extracted 3 memories from session` — one of them "User prefers Umni",
  produced by the regex fallback (`I like X`) even though the model had judged
  the window and returned nothing durable.
- `Memory audit complete: 113 -> 112`, then `DELETE odysseus_memories` (404),
  `DELETE odysseus_memories_custom` (404), `DELETE odysseus_memories_fastembed`
  (200), `POST collections`, two `add` batches, `MemoryVectorStore rebuilt
  with 112 entries`: the whole collection dropped and every entry re-embedded
  to remove one; memory search returned nothing to any concurrent turn in
  between. The audit counter that triggered it is shared across owners.
- The `Memory Tidy` task saved the JSON store without touching the vector
  index, so entries it removed kept surfacing in retrieval.
- `[cal-extract] … Updated event → G FUEL Labor Day sale ends`: calendar
  extraction has no sender or content gate; `_sender_is_automated` existed
  but was wired only to away-replies, and classification ("marketing") runs
  after extraction in the same loop.
- Vault: reads go through `search_documents` as the operating model says.
  Outcome recording into `AI Mind` existed only for the local Pi worker; the
  Claude Code delegation skill had no equivalent. `vault_search`/`vault_get`
  are the Bitwarden password-vault tools and are easy to confuse with the
  Markdown vault.

## Priorities

1. `MemoryVectorStore.sync(memories)`: reconcile in place — delete ids that
   are gone, re-embed changed text, add new ids. The audit calls it instead of
   `rebuild`; `rebuild` stays for cold start only.
2. Per-owner audit counter (`_extractions_since_audit` is a dict).
3. When the model ran and judged the window, keep only identity facts from
   the regex fallback; preferences/goals come from the model or not at all.
   The fallback still fills in fully when the model call failed.
4. `Memory Tidy` removes dropped ids from the vector index and adds any it
   kept, after saving.
5. Calendar extraction skips list/bulk/no-reply senders and promotional
   subjects (`_not_calendar_material`). Summaries, tags and urgency still run.
6. The Claude Code delegation skill records accepted work in
   `AI Mind/Claude Code Delegation.md` under the same policy as the Pi path.

## Expected effect

An audit after N added memories costs one delete and a handful of embeds
instead of a full drop and ~110 re-embeds, with no empty-search window.
Brainstorm chats stop minting preference memories. Promotional mail stops
creating calendar events. Both delegation paths leave the same kind of
orientation record in the vault.

## Acceptance criteria

- `tests/test_harness_efficiency_specs.py`: `sync` reconciles a changed, a
  new and a stale entry with exactly two embeds and no collection drop; the
  audit calls `sync`, not `rebuild`; the counter is per owner; promotional
  and list mail is not calendar material while a dentist confirmation is.
- `tests/test_memory_fallback_dislike.py` still passes (the fallback's
  sentiment logic is unchanged; only when it applies changed).
- Existing memory provider, consolidation and import tests pass.

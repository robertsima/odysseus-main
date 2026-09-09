# Context harness efficiency and recall continuity

## Goal

Make Odysseus a more reliable, token-efficient harness for coding, research,
multi-agent work, automation, and planning when the useful state is distributed
across a long chat, tool output, saved memory, and a document knowledge base.

## Evidence from the observed workflow

The conversation required repeated attempts to complete one bounded repository
change and publish it:

1. The assistant first claimed GitHub access but later said shell was absent,
   then acknowledged shell access.
2. Two earlier implementation loops were completed and eventually published,
   but a later context-harness branch was reported as implemented without being
   persisted or pushed. The next session could not locate it, so the work had to
   be recreated.
3. Short follow-ups such as "do this again", "try again", and "App Token is
   available" depended on the preceding user goal. Retrieval operated on the raw
   short message, which is a poor semantic query and returned either unrelated
   context or no actionable repository context.
4. Vault retrieval was useful for confirming the deployed fork, branch, RAG
   architecture, and AI Mind/Vault Mind boundary. It was not sufficient for the
   requested implementation: the retrieved deployment audit was historical and
   the exact current behavior had to be verified in source.
5. Dynamic memory, RAG, web, and skills messages are currently prepended before
   persisted history. That changes the early prompt on every turn, reducing API
   prompt-cache/KV-cache reuse. Those ephemeral messages also enter automatic
   compaction, allowing one turn's retrieval results to consume summary tokens
   or become stale retained state.
6. Context blocks have independent character limits, but no shared token budget.
   RAG, memories, skills, transcripts, and web results can collectively crowd out
   the user's goal and recent conversation.
7. Agent metrics already capture provider cache usage and tool rounds, but the
   context builder does not expose retrieval-query strategy or token attribution,
   making recall misses and expensive prompt composition hard to diagnose.

## High-value changes

### 1. Follow-up-aware retrieval query

When the current request is short and referential, build the memory/RAG query
from the preceding substantive user request plus the current request. Never use
assistant text, tool output, or injected context to expand retrieval. Concrete
requests continue to use their own text unchanged.

Examples:

- `do this again` -> `<previous user request>\nFollow-up: do this again`
- `try again` -> `<previous user request>\nFollow-up: try again`
- `find my notes about caching` -> unchanged

### 2. Cache-stable prompt layout

Keep stable system instructions first, followed by the complete persisted
conversation and current user request. Append dynamic memory/RAG/web/skills/
transcript context at the request tail. Because the active request is persisted
but dynamic retrieval is not, tail placement retains the longest possible
byte-identical prefix on the next turn for provider prompt caches or local KV
reuse. Inserting ephemeral context before the request would permanently break
the prefix at the first retrieval-enabled turn.

### 3. Ephemeral-context isolation and shared token budget

Run automatic conversation compaction on stable instructions and persisted
history only. Never summarize request-local retrieval into durable conversation
state. After compaction, allocate all dynamic context from one adaptive budget:
at most 20% of the model window, capped at 12,000 estimated tokens and by the
actual remaining room after response reserve. Share the budget across sources,
redistributing unused capacity, so one source cannot evict all others.

### 4. Context composition diagnostics

Attach compact diagnostics to the request/response metrics:

- retrieval mode (`current` or `follow_up`)
- static, history, and dynamic estimated token counts
- dynamic budget, source token attribution, and whether truncation occurred
- number of RAG sources and memories used

Do not include retrieved text or the expanded query in diagnostics.

## Non-goals

- No semantic-result cache in this change. Document indexes and user context can
  change; caching retrieval safely requires index/version-aware keys and should
  be designed separately.
- No provider-specific price table. Cache token counters are retained as neutral
  usage facts because pricing changes independently of the application.
- No change to the trust boundary: retrieved and external content remains
  user-role, explicitly fenced, untrusted data.

## Acceptance criteria

1. Short referential follow-ups retrieve with prior user intent; ordinary queries
   are unchanged.
2. Dynamic context appears after the persisted conversation and current user
   request; system context remains first and the cacheable prefix is undisturbed.
3. Automatic compaction never receives dynamic retrieval messages.
4. Dynamic context stays within its computed aggregate token budget and retains
   representation from multiple non-empty sources when the budget permits.
5. Internal context metadata is stripped before provider calls.
6. Direct-chat and agent metrics include context diagnostics without source text.
7. Existing compaction, prompt-safety, RAG, chat-helper, and agent accounting
   tests continue to pass.

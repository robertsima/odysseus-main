# Bounded recursive context compaction

## Problem

Each automatic compaction retained all prior summary system messages and added a
new summary. Repeated compactions therefore grew the prompt indefinitely, spent
more utility-model input tokens, and repeatedly invalidated a larger provider
prefix cache. Simply deleting old summaries would lose the state they preserve.

## Behavior

- Include prior compacted context when producing the next summary.
- Replace all prior compaction summaries with the new consolidated summary.
- Bound utility-model summary input to 4096 estimated tokens.
- Retain ordinary system messages and recent unsummarized conversation turns.
- Apply the same replacement rule to persisted session history.

## Verification

Tests verify one-summary output after recursive compaction, preservation of old
and newly summarized context in the utility prompt, bounded prompt size, and
replacement of persisted prior summaries.

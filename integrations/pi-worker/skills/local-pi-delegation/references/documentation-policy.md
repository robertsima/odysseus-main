# Delegation log policy

Applies only when the owner keeps a delegation log: a folder of the knowledge base that `AGAMEMNON_PI_DOCUMENTATION_ROOT` (legacy `ODYSSEUS_PI_DOCUMENTATION_ROOT`) points `record_pi_task` at. Without one, skip the record.

## Boundary

Write accepted delegation records only to that folder. Unrelated vault content is never written, moved or deleted.

The folder sits inside the personal-documents tree that `search_documents` indexes. It is not a bound workspace and not under `/app/workspace`. `record_pi_task` already knows where it is. For another note in the folder that you update yourself, take the path from a `search_documents` result, never from the folder name.

The standard note is `Local Model Delegation.md` in that folder. `record_pi_task` owns the append and uses a fixed filename beneath the configured documentation root.

## Acceptance record

Record only after you inspected the result and judged it sufficient. Include:
- project and short task title;
- concise outcome;
- files changed;
- tests or checks you reviewed;
- known limitations or follow-up.

Keep out chain-of-thought, credentials, private data, large diffs, pasted source files and raw command logs. The record should orient a later session without recreating the implementation conversation.

When a more specific existing note in the log folder also needs updating, use `edit_file` or `write_file` on the path `search_documents` returns for it, after the acceptance record. Keep the note's structure and the summary brief.

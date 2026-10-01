# AI Mind documentation policy

## Boundary

Write accepted delegation records only to the `AI Mind` folder of the user's knowledge base. `Vault Mind` and `Journal` content is never written, moved or deleted.

`AI Mind` sits inside the personal-documents tree that `search_documents` indexes. It is not a bound workspace and not under `/app/workspace`. `record_pi_task` already knows where it is. For another `AI Mind` note you update yourself, take the path from a `search_documents` result, never from the folder name.

The standard note is `AI Mind/Local Model Delegation.md`. `record_pi_task` owns the append and uses a fixed filename beneath the configured documentation root.

## Acceptance record

Record only after you inspected the result and judged it sufficient. Include:
- project and short task title;
- concise outcome;
- files changed;
- tests or checks you reviewed;
- known limitations or follow-up.

Keep out chain-of-thought, credentials, private data, large diffs, pasted source files and raw command logs. The record should orient a later session without recreating the implementation conversation.

When a more specific existing `AI Mind` note also needs updating, use `edit_file` or `write_file` on the path `search_documents` returns for it, after the acceptance record. Keep the note's structure and the summary brief.

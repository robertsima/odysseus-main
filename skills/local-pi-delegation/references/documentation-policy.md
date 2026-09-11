# AI Mind Documentation Policy

## Boundary

Write accepted delegation records only to the `AI Mind` folder of the user's knowledge base. Never write, move, or delete content in `Vault Mind` or `Journal`.

`AI Mind` is a folder inside the personal-documents tree that `search_documents` indexes — not a bound workspace and not under `/app/workspace`. `record_pi_task` already knows where it is; when you touch another `AI Mind` note yourself, take its path from a `search_documents` result rather than building one from the folder name.

The standard note is `AI Mind/Local Model Delegation.md`. `record_pi_task` owns the append operation and uses a fixed filename beneath the configured documentation root.

## Acceptance Record

Record only after the primary harness has inspected the result and judged it sufficient. Include:

- project and short task title;
- concise outcome;
- files changed;
- tests or checks reviewed by the harness;
- known limitations or follow-up.

Do not store chain-of-thought, credentials, private data, large diffs, pasted source files, or raw command logs. A record should be useful for later orientation without recreating the implementation conversation.

When a more specific existing `AI Mind` note should also be updated, use Odysseus's normal file tools (`edit_file`, `write_file`) on the path `search_documents` returns for it, after the acceptance record. Preserve the note's existing structure and keep the summary brief.

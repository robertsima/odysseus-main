# AI Mind Documentation Policy (Claude Code delegation)

Same rules as the local Pi delegation policy, applied to work Claude Code did.

## Boundary

Write accepted delegation records only to the configured `AI Mind` workspace.
Never write, move, or delete content in `Vault Mind` or `Journal`.

The standard note is `AI Mind/Claude Code Delegation.md`. There is no
dedicated record tool for this path: locate the note with `search_documents`
and append with `edit_file` (or `write_file` the first time). Append a
`## <ISO timestamp> — <project>: <task>` block; do not rewrite earlier blocks.

## Acceptance record

Record only after the primary harness has inspected the diff and the test
evidence and judged the result sufficient. Include:

- project (repository path) and short task title;
- concise outcome and the commit hash;
- files changed (from the tool's `changed_files`, not from Claude's summary);
- tests or checks reviewed by the harness;
- known limitations, denied permissions worth knowing, or follow-up.

Do not store chain-of-thought, prompts, credentials, private data, large
diffs, pasted source files, or raw command logs. A record should be useful for
later orientation without recreating the implementation conversation.

When a more specific existing `AI Mind` note should also be updated (a project
note, a decision log), use the normal workspace file tools after the
acceptance record and preserve the note's structure.

# AI Mind documentation policy (Claude Code delegation)

Same rules as the local Pi delegation policy, applied to work Claude Code did.

## Boundary

Write accepted delegation records only to the `AI Mind` folder of the user's knowledge base. `Vault Mind` and `Journal` content is never written, moved or deleted.

`AI Mind` sits inside the personal-documents tree that `search_documents` indexes. It is not a bound workspace and not under `/app/workspace`. Take its real path from a `search_documents` result (every hit carries the source file's absolute path) or from the path a refused `write_file` or `ls` call suggests. The folder name alone is never the path. When a file tool refuses a path, do not write the note through the shell: that puts the record outside the vault where the index never sees it.

## Recording

The standard note is `AI Mind/Claude Code Delegation.md`. No dedicated record tool exists for it:
1. `search_documents` for "Claude Code Delegation" to find the note.
2. Append with `edit_file` a `## <ISO timestamp> — <project>: <task>` block, leaving earlier blocks as they are. If the search finds nothing, `write_file` the note first, under the `AI Mind` path the search results show.

Record only after you inspected the diff and the test evidence and judged the result sufficient. Include:
- project (repository path) and short task title;
- concise outcome and the commit hash;
- files changed (from the tool's `changed_files`, not Claude's summary);
- tests or checks you reviewed;
- known limitations, denied permissions worth knowing, or follow-up.

Keep out chain-of-thought, prompts, credentials, private data, large diffs, pasted source files and raw command logs. The record should orient a later session without recreating the implementation conversation.

When a more specific `AI Mind` note (a project note, a decision log) also needs updating, do it after the acceptance record with `edit_file` or `write_file` on the path `search_documents` returns for it, and keep that note's structure.

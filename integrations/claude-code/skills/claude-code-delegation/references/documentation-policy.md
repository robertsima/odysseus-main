# Delegation log policy (Claude Code delegation)

Same rules as the local Pi delegation policy, applied to work Claude Code did. They apply only when the owner keeps a delegation log, an explicitly configured knowledge-base folder. If `search_documents` finds no such folder or note, skip the record and do not create the folder.

## Boundary

Write accepted delegation records only to that folder. Unrelated vault content is never written, moved or deleted.

The folder sits inside the personal-documents tree that `search_documents` indexes. It is not a bound workspace and not under `/app/workspace`. Take its real path from a `search_documents` result (every hit carries the source file's absolute path) or from the path a refused `write_file` or `ls` call suggests. The folder name alone is never the path. When a file tool refuses a path, do not write the note through the shell: that puts the record outside the vault where the index never sees it.

## Recording

The standard note is `Claude Code Delegation.md` in the log folder. No dedicated record tool exists for it:
1. `search_documents` for "Claude Code Delegation" to find the note.
2. Append with `edit_file` a `## <ISO timestamp> — <project>: <task>` block, leaving earlier blocks as they are. If the log folder exists but the note does not, `write_file` the note first, in the log folder the search results show.

Record only after you inspected the diff and the test evidence and judged the result sufficient. Include:
- project (repository path) and short task title;
- concise outcome and the commit hash;
- files changed (from the tool's `changed_files`, not Claude's summary);
- tests or checks you reviewed;
- known limitations, denied permissions worth knowing, or follow-up.

Keep out chain-of-thought, prompts, credentials, private data, large diffs, pasted source files and raw command logs. The record should orient a later session without recreating the implementation conversation.

When a more specific note in the log folder (a project note, a decision log) also needs updating, do it after the acceptance record with `edit_file` or `write_file` on the path `search_documents` returns for it, and keep that note's structure.

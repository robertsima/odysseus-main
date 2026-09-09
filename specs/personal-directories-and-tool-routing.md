# Personal directories and tool-routing specification

## Status

Proposed implementation specification for the harness reliability work.

## Personal-directory ownership

`ODYSSEUS_PERSONAL_DIRS` describes application-managed document roots. It is
configuration for indexing, not a user-owned resource declaration. Startup
reconciliation MUST NOT require `auth.json`, an admin account, or a request
owner to register and index a declared directory.

The indexer MUST preserve the existing sensitivity label and path-confinement
rules. A malformed sensitivity label, missing mount, or path outside
`PERSONAL_DIR` remains a per-entry error and MUST fail closed. A valid mounted
entry is tracked and indexed with no `owner` metadata. Search MUST therefore
remain able to retrieve these chunks when the caller has the applicable
personal-document access.

This is intentionally single-scope application data. If multi-user isolation
is introduced later, ownership must be designed as a separate migration and
must not be inferred from whichever account happens to exist at startup.

## Tool-selection policy

Each turn selects tools from the current request, deterministic domain rules,
retrieval, and explicitly forced tools. Conversation history is context, not
a blanket request to retain every tool previously used.

Previously used tools MAY be retained when they are relevant to the current
turn or the current turn is an explicit continuation of an established task.
Generic execution tools (`bash`, `python`, `run_shell`, and `shell`) MUST NOT
be retained solely because they appeared earlier. They may be retained when
the current intent is `workspace` or `shell`, the active workspace establishes
an ongoing coding task, or the user explicitly asks for terminal/command
execution. Named filesystem, log, web, and application tools remain preferred.

Route-level disabled tools always win. Active-document and plan-mode pruning
runs after retention. The final set must be logged as a summary, never with
arguments or document contents.

## Selection telemetry

Every agent turn SHOULD emit structured, privacy-safe routing telemetry:

- retrieval source (`caller`, `rag`, `keyword`, `always_available`, or
  `low_signal_workspace`);
- detected domains and matched tools;
- selected-tool count;
- retained tool count;
- retained generic execution tools suppressed by policy;
- disabled-tool count;
- selection duration.

Telemetry MUST contain tool names and counts only. It MUST NOT contain tool
arguments, outputs, prompts, document paths from user content, or private
retrieved excerpts. Existing final metrics SHOULD expose the same scalar
summary so routing can be compared with response cost and tool-call counts.

## Acceptance criteria

1. A valid `ODYSSEUS_PERSONAL_DIRS` declaration indexes successfully when
   `auth.json` is absent.
2. A declared private directory remains private and a bad label is skipped.
3. A follow-up email or document task does not inherit `bash` merely because
   an earlier turn used it.
4. A follow-up workspace or explicit terminal task can retain `bash`.
5. Disabled tools are never reintroduced by retention.
6. Logs and final metrics expose routing decisions without sensitive payloads.

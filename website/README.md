# Documentation

Two kinds of document live in this repository, and the difference matters when
you are deciding which one to trust or which one to update.

- **`docs/` — how it is.** Descriptive. Written against the code that is on
  `dev` right now, and stale the moment the code moves. If a document here
  disagrees with the source, the source wins and the document is a bug.
- **`specs/` — how it must behave.** Prescriptive. A spec states a rule the
  code has to satisfy, usually because breaking it already cost something
  once. If the code disagrees with a spec, the code is the bug.

Start here depending on what you are trying to do.

## I want to run it

| Document | What it covers |
|---|---|
| [`setup.md`](setup.md) | Install and deployment: Docker, native macOS/Windows, GPU, HTTPS, troubleshooting |
| [`configuration.md`](configuration.md) | What belongs in `.env` versus the Settings UI, and why there are two systems |
| [`backup-restore.md`](backup-restore.md) | What in `data/` is irreplaceable (starting with `data/.app_key`) and how to move it |
| [`email-outlook.md`](email-outlook.md) | The Outlook/O365 app-password situation |
| [`security-ci.md`](security-ci.md) | What the automated security checks on a pull request do, and which can be waived |

## I want to understand the system

Read these four in order; each assumes the one before it.

| Document | What it covers |
|---|---|
| [`architecture-runtime.md`](architecture-runtime.md) | The machine: what runs where, a request end to end, the module layering measured from the import graph, the authoritative inventory of where state lives, the no-build-step frontend, and the concurrency model |
| [`subsystems.md`](subsystems.md) | The features: what each area owns, where its code and data live, what it depends on |
| [`agent-runtime.md`](agent-runtime.md) | The agent turn: how the tool list for a round is chosen, what the prompt is made of, how context is kept bounded, and which guards stop a loop |
| [`design-patterns.md`](design-patterns.md) | The recurring decisions across all of the above, and the failure each one was paid for by |

`architecture-runtime.md` answers "where does this live and who writes it".
`subsystems.md` answers "which files implement feature X". `agent-runtime.md`
answers "why did the model call that tool, or fail to". `design-patterns.md`
answers "why is it written that way" — read it before a refactor, since most
of what looks redundant in this codebase is load-bearing.

## I want to work on a specific area

| Document | Area |
|---|---|
| [`vault-retrieval.md`](vault-retrieval.md) | Indexing, ranking and path rules for the Markdown vault |
| [`attachments.md`](attachments.md) | Upload storage and the stable references passed through chat history |
| [`agent-worktree.md`](agent-worktree.md) | The agent's Git worktree and the human gate in front of publishing |
| [`workbench.md`](workbench.md) | The observability window: activity, changes, commits, pull requests |
| [`agent-migration.md`](agent-migration.md) | Importing another agent's state without trusting it wholesale |
| [`harness-operating-model.md`](harness-operating-model.md) | How the harness is meant to be operated |
| [`pr-blocker-audit.md`](pr-blocker-audit.md) | `scripts/pr_blocker_audit.py`, the read-only PR overlap triage helper |

## Behaviour specs

`specs/` holds the rules. They are short and worth reading before changing the
subsystem each one governs, because each was written after the behaviour broke:

- [`specs/architecture-runtime-inventory.md`](../specs/architecture-runtime-inventory.md) — module-level structural baseline: file sizes, importer counts, refactor candidates
- [`specs/prompt-prefix-stability.md`](../specs/prompt-prefix-stability.md) — what may and may not change in the cached prompt prefix between rounds
- [`specs/tool-schema-hygiene.md`](../specs/tool-schema-hygiene.md) and [`specs/tool-schema-cost-ledger.md`](../specs/tool-schema-cost-ledger.md) — what a tool schema costs per round and what is allowed to be bound
- [`specs/context-harness-efficiency.md`](../specs/context-harness-efficiency.md), [`specs/harness-reliability-efficiency.md`](../specs/harness-reliability-efficiency.md), [`specs/bounded-recursive-compaction.md`](../specs/bounded-recursive-compaction.md) — the context budget and how compaction stays bounded
- [`specs/retrieval-runtime.md`](../specs/retrieval-runtime.md) — embedding lanes and collection fingerprints
- [`specs/personal-directories-and-tool-routing.md`](../specs/personal-directories-and-tool-routing.md) — which directories the file tools may reach
- [`specs/memory-vault-email-hygiene.md`](../specs/memory-vault-email-hygiene.md), [`specs/auto-name-failure-backoff.md`](../specs/auto-name-failure-backoff.md)

## Other files in this folder

`index.html` is the hover-to-play landing-page tour linked from the front
README; the images beside it (`odysseus-wordmark.png`, `odysseus-browser.jpg`
and the rest) are its assets and the README's.

## Keeping these honest

These documents quote real paths, constants and counts so a claim can be
checked rather than believed. That is also what makes them go stale. Where a
number is likely to drift, the document says how to recompute it — run that
command instead of trusting the figure. When you change behaviour a document
here describes, update it in the same pull request; a wrong document costs
more than a missing one.

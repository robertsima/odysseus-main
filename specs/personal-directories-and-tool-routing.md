# Personal directories, model access, and agent-control specification

## Status

Living specification. The original harness-reliability requirements remain in
force. The vault, configuration, delegation, and per-agent control-plane work
described below is implemented on `dev` as of 2026-09-16 unless explicitly
marked as a follow-up.

## Product intent

Odysseus must be deployable on a workstation, server, container host, or NAS
without inheriting the original operator's paths, binaries, SSH aliases, model
providers, or security assumptions. Runtime environment variables describe the
machine or deployment. User-selectable behavior belongs in the admin settings
store and its schema-driven UI.

The Markdown vault is the canonical human-readable note store. Notes and other
vault files share one index and one access-policy system. Agent delegation is
provider-neutral: Claude, local models, subscription-backed providers, MCP
servers, and other Odysseus agents are capabilities rather than hard-coded
special cases.

## Implemented baseline — 2026-09-16

### Configuration and capability discovery

- A capability registry declares optional subsystems, requirements, tools, and
  availability. Unavailable capabilities are not advertised as usable tools.
- Admin configuration is described by a settings schema and grouped into
  navigable categories with descriptions, safe defaults, and advanced fields.
- Finite settings use selects. Live inventories populate endpoint and model
  selects. Values that allow provider-specific extensions use suggestions while
  retaining a custom-value escape hatch. Paths, secrets, prompts, and arbitrary
  structured data remain typed free-form controls.
- Capability details are collapsed by default. Requirement rechecks use the
  dedicated `/api/capabilities/recheck` route, and the advanced-settings action
  opens a category that actually contains advanced fields.
- Settings chosen by an administrator belong in the settings store. Environment
  variables remain appropriate for bootstrapping, host paths, ports, mounts,
  secrets supplied by an orchestrator, and deployment-level feature gates.

### Markdown vault and note policy

- Legacy database notes can be migrated to editable Markdown with a reversible
  dry run. External edits must round-trip without length-prefix or hidden-body
  assumptions.
- Mounted Markdown files appear in the Notes vault explorer and can be opened
  and edited by a signed-in human. The tree is collapsed initially, uses clear
  folder depth, and scales to large vaults without expanding every directory.
- `public` and `private` control model/agent reads. `readonly` controls
  model/agent writes and is independent of sensitivity. These policies do not
  prevent an authorized human from editing through the UI.
- Folder policy inherits to descendants. The deepest folder declaration wins,
  and a file may override sensitivity in its own frontmatter. Read-only may be
  explicitly inherited or cleared by a child policy.
- All vault content uses one embedding/indexing pipeline. Public chunks may be
  retrieved normally; private chunks require an explicit private-vault read
  grant on the active model, agent profile, or scoped external token.
- Malformed policy fails closed. Path confinement applies to direct file tools
  as well as semantic retrieval so search and file access cannot disagree.

### Agent collaboration and loadouts

- Agent-to-agent work is provider-neutral and supports non-blocking peer
  messages in addition to request/response delegation. A normal information
  request must remain with the current agent unless delegation is useful and
  allowed by its policy.
- The Agent Control Room is an integrated, dockable workspace for concurrent
  runs. Monitoring and configuration are separate views so the loadout editor
  is not squeezed into the telemetry sidebar.
- Each agent has enforceable controls for profile/preset, approval behavior,
  delegation policy, parallel-worker limit, tools, skills, memory access,
  available models, MCP/integration access, and private-vault access.
- Loadout editing is organized into Behavior, Tools, Knowledge, and Models & MCP
  tabs. The monitoring split is resizable, the window can expand, and compact
  docked layouts stack cleanly. Opening chat or workbench does not implicitly
  close the control room.
- Navigation order is user-reorderable and stored as a preference.
- An agent may author and start worker loadouts (`manage_agent_loadout`). Every
  capability in a loadout an agent creates is intersected with the calling
  chat's own effective policy before it is stored, and each narrowing is
  reported back to the agent. An agent cannot write itself a loadout wider than
  it has.

### Account-scoped interface preferences

- Look-and-feel and layout choices belong to the signed-in account, not to the
  browser: theme (including font, density, background pattern and effects,
  frosted glass and text size), saved custom themes, and navigation order are
  persisted through `/api/prefs`, which keys by signed-in user.
- localStorage remains the first-paint cache. Both copies carry a write time
  and are reconciled newest-wins on every boot, so a second browser adopts the
  account's choice instead of keeping whatever it happened to store first. A
  tie resolves to the account copy.

## Required invariants

1. Human file access and model policy are distinct. A `readonly` badge means
   “agents cannot write,” not “the signed-in user cannot edit.”
1a. An agent-authored loadout is a narrowing of the authoring chat's policy,
   never an extension of it. This is an authoring rule; what a worker may
   actually do is still decided at execution time by its own stored policy and
   the owner baseline.
2. Private content never enters a prompt, tool result, memory extraction pass,
   or delegated task without an explicit private-read grant.
3. A grant is checked at read time. Indexing private material in the shared
   store does not make it public, and changing a policy takes effect without
   requiring a second public/private database.
4. Per-agent restrictions are enforced server-side. Hiding a checkbox or tool
   in the browser is not an authorization boundary.
5. A preset is a reusable starting point; the persisted agent profile is the
   effective policy and must survive reopening the control room.
6. Unknown or unavailable capabilities degrade honestly with an explanation
   and setup action. They must not fail later as phantom tools.
7. Settings controls preserve saved values that are temporarily unavailable,
   clearly marking them rather than silently replacing them.

## Follow-up product requirements

These extend the implemented baseline and should be tracked in the roadmap:

- Versioned loadout presets with clone, import/export, diff, reset, and an
  effective-policy preview before saving.
- Per-run temporary grants with expiry, grant reason, issuer, revocation, and a
  visible audit trail—especially for private vault access and write-capable
  tools.
- A live agent topology view showing parent/child relationships, peer messages,
  blocked approvals, token/cost/context budgets, and critical-path progress.
- Agent resource limits: maximum turns, wall time, parallel children, context
  budget, tool-call budget, and optional provider spend limits.
- Vault reindex status, policy-aware search preview, conflict handling for
  external edits, file creation/move/rename, keyboard navigation, and favorites.
- A policy inspector that answers “why can this agent read/write this file or
  use this tool?” using the exact inheritance and profile rules applied by the
  backend.
- Better setting widgets for directory picking, validated URL/host fields,
  connection tests, secret replacement, dependency installation, and clear
  “restart required” indicators.
- Role templates for administrator, standard human, local trusted agent,
  hosted model, research agent, and untrusted external integration while full
  multi-user isolation continues to mature.

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

The [capability routing refactor](harness-capability-routing.md) makes this
distinction explicit: active-document relevance is advisory; privacy, profile,
global/session disables and read-only policy are authorization boundaries.
`discover_tools` can load an unselected permitted tool, but cannot connect a
server, grant access or execute the requested operation. Initial schemas and
all late additions share bounded accounting; explicit user/caller bindings are
preserved and any advisory-budget override is reported. Fresh global and
session revocations are enforced again at dispatch.

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

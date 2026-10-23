# Task-first UI

The app is organized around what a person is trying to do, not around its
storage systems or agent machinery. A person should finish ordinary work without
knowing documents from vault files, loadouts from workers, or which tool reads
what. Advanced controls stay, one click deeper.

The test journey for every change here:

> Open Tall Tree → select DA-02 → ask the SDS agent to revise it → answer one
> clarification → inspect the changes → export a PDF → find it again later.

It must be possible without choosing Admin, typing a filesystem path, naming a
tool, or wondering which copy is current.

## Vocabulary

Use these words in UI text, and no others for the same idea.

| Word | Means | Not |
|---|---|---|
| Library | The one place to find work: notes, documents, files, projects | Archive, Vault browser, Documents panel |
| Note | A Markdown note (Notes store) | |
| Document | An editable document in the app's editor (DB) | Editor document, doc record |
| File | A file in the vault or an upload: PDF, Word, image, Markdown outside Notes | Vault file, personal doc |
| Project | A folder under a vault `Projects/` folder, e.g. `AI Mind/Projects/Tall Tree Group` | Workspace (that is a code checkout) |
| Agent | A saved specialist (loadout) | Loadout, profile, worker, crew |
| Talk to agent | Open a chat where that agent answers | Apply loadout |
| Assign a task | The agent works separately and its result returns to this chat | Launch worker, delegate, spawn |
| Working master | The editable copy that is the source of truth | |
| Export | A copy made from the master (PDF, Word copy) | |
| Signed copy | An executed/signed file. Locked, never edited | |
| History | Earlier revisions and backups | Backups, revisions |
| Supporting file | Previews, receipts, hashes, HTML renders | |

Technical names (tool names, paths, loadout keys) appear only under an
**Advanced** disclosure or in a tooltip.

## Artifact roles (backend, one classifier)

`src/artifact_roles.py` is the single place that decides a file's role. The
Library and the vault tree both use it; the UI never re-derives a role.

- `signed`: the name contains `signed`, `executed` or `countersigned` (any
  case). Locked.
- `history`: any path segment is a history folder: `Backups`, `Backup`,
  `Archive`, `Archived`, `History`, `Revisions`, `Old`, `Review Packets`,
  `.trash` (any case), or the name carries a backup marker (`.bak`, `~` suffix,
  `(1)`-style copy suffix).
- `supporting`: `.html`, `.htm`, `.json`, `.sha256`, `.md5`, `.txt` receipts,
  and anything in a `Previews`/`Receipts` folder.
- `export`: `.pdf` (not signed), and a Word file whose stem also has a Markdown
  master next to it.
- `master`: an editable file (`.md`, `.markdown`, `.docx`) that is not one of the
  above. When `.md` and `.docx` share a stem, the `.md` is the master and the
  `.docx` is an export; if the `.docx` is newer, the group carries the warning
  `word_newer_than_master`.
- `source`: images and anything in an `Originals`/`Source`/`Uploads`/`Inbox`
  folder.

**Group** = files in one folder that share a normalized stem (lowercase, strip
extension, strip trailing ` v2`, `-v3`, ` (1)`, ` copy`, `_signed`, a trailing
date or timestamp like `-20261021` / ` 2026-10-21`). History items attach to the
group with the same stem in the nearest non-history ancestor folder.
Each group exposes: `title`, `master`, `current_export` (newest export),
`signed[]`, `history[]` (newest first), `supporting[]`, `warnings[]`.

Nothing is moved, renamed or deleted to produce this view.

## Backend contracts

### Library: `routes/library_routes.py`

`GET /api/library/items?kind=all|note|document|file|project&project=&q=&limit=&offset=`

Human signed-in user only (reject the agent loopback user, as the vault routes
do). Documents are owner-scoped with the existing document helpers. Notes use
`STORE.list(owner)`. The vault is shared by every signed-in human (as today);
the response says so with `"vault_shared": true`.

```json
{
  "items": [{
    "id": "doc:<uuid> | note:<id> | file:<vault path> | group:<folder>/<stem>",
    "kind": "note|document|file|group",
    "title": "Tall Tree — Initial Server Setup",
    "type": "markdown|pdf|docx|image|other|document|note",
    "role": "master|export|signed|source|history|supporting|null",
    "project": "Tall Tree Group | null",
    "breadcrumb": ["Projects", "Tall Tree Group", "Agreements"],
    "path": "<vault path, files only>",
    "updated_at": "ISO-8601",
    "locked": false,
    "group": {"master": {...item}, "current_export": {...item}, "signed": [], "history": [], "supporting": [], "warnings": []},
    "actions": ["open", "edit", "download", "history", "related"]
  }],
  "total": 0, "projects": ["Dono Fuel", "Tall Tree Group"], "vault_shared": true
}
```

`kind=all` returns groups (one entry per group, not per file) plus documents
and notes. `kind=file` returns groups too. `history` and `supporting` files are
never top-level items.

`GET /api/library/projects` → `{"projects": [{"name", "path", "counts": {"documents", "files", "notes"}, "updated_at"}]}`.

A document belongs to a project when its title starts with the project name
(case-insensitive, ignoring punctuation). A note belongs when its label or a
`#project/<name>` tag matches.

The vault walk is cached in-process (short TTL, keyed on vault root, cleared by
vault and note writes) and runs off the event loop. Listing does not read file
contents.

The vault tree (`GET /api/personal/vault/tree`) gains `role` on file nodes from
the same classifier.

### Agent access and actions: `routes/agents_routes.py`

`GET /api/agents/profiles/{name}/access?session_id=&task=`

Owner-scoped like the rest of `/api/agents`; read-only, not admin-only. It
composes existing resolvers (`caller_policy`, `profile_readiness`,
`cap_to_starter`, `worker_withheld_rows`, `tool_capabilities`), with the
sentence logic in `src/loadout_access_summary.py`, a formatter and not a policy
engine.

```json
{
  "name": "SDS Consulting Lead", "description": "...",
  "talk":   {"summary": "Can read and edit project files and search indexed notes. Asks before external actions.", "facts": [{"level": "can|cannot|asks", "text": "..."}]},
  "assign": {"summary": "... Results return to this chat.", "facts": [...], "blocker": null},
  "advanced": {"tool_access": "...", "tools": [...], "skills": [...], "shell": "...", "delegation": "...", "approval": "...", "withheld": [...]},
  "quick_actions": [{"label": "Revise agreement", "prompt": "..."}]
}
```

`blocker` (only when `task` is given and preflight or the document check would
refuse it) is `{"code", "summary", "detail", "actions": [...]}`.

`POST /api/agents/profiles/{name}/chat` creates a new chat owned by the caller
with the agent applied (same patch and guard as `POST /sessions/{id}/loadout`)
and returns `{"session_id"}`.

`POST /api/agents/launch` accepts `requires` and `extra_tools` (as the agent
tool does) and, when blocked, returns status 409 with
`{"error", "code", "blocker": {"code","summary","detail","actions"}}` instead of
a flattened string.

**Recovery actions** are objects `{"id", "label", ...args}`. Offer only what
the policy permits; never "switch to Admin".

| Blocker | Actions |
|---|---|
| WORKSPACE_REQUIRED / WORKSPACE_INVALID | `choose_workspace` (with `candidates`), `run_without_files` |
| TOOLS_UNAVAILABLE / WRITE_NOT_ALLOWED | `allow_for_this_run` (`extra_tools`, only tools the starter itself holds), `read_only`, `pick_another_agent` |
| SHELL_NOT_AVAILABLE | `pick_another_agent`, `analysis_only` |
| DOCUMENT_UNREADABLE (document check) | `attach_excerpt` (paste a read-only copy into the task), `allow_for_this_run` (the reading tool, if permitted), `choose_accessible_file` |
| capacity / depth | `wait`, `open_agents` |

Agent profiles gain an optional `quick_actions: [{label, prompt}]` (max 6).
Prompts may contain `{project}` and `{document}` placeholders that the UI fills
from the current context.

### Chat context: `routes/session_routes.py`, `routes/chat_routes.py`

Session settings accept `doc_target`: `document | project | general`.

`GET /api/session/{id}/settings` adds:

```json
{"context": {"who": {"kind": "agent|persona|default", "name": "..."},
             "project": "Tall Tree Group | null",
             "document": {"id": "...", "title": "..."} | null,
             "doc_target": "document|project|general",
             "access": {"summary": "File editing allowed · External actions require approval", "files": "edit|read|none", "external": "auto|ask|ask_all", "shell": "off|sandbox|host"}}}
```

`project` = the project folder of the chat's workspace or active document, else
the chat's sidebar folder, else null.

`chat_stream` honors `doc_target`: for `project` and `general` it does not
inject or rebind the open document (all four lookup stages), does not set the
active document for argument-less document tools, and does not switch the chat
to agent mode because a document is open. Approval continuations are exempt.

## Frontend

### Shell and navigation

The sidebar shows three groups:

- **Work**: Today, Library, Notes, Tasks, Calendar
- **Agents**: Agents, Workbench (when enabled)
- **More** (collapsed by default, remembers its state): Brain, Lotus, Compare,
  Cookbook, Deep Research, Gallery

Theme moves to the footer next to Settings. The sidebar Command dropdown is
removed (its items are already in the sidebar); the rail keeps its own. Every
item stays reachable, reorderable within its group and hideable in Customize UI.
No feature is deleted.

### Library

The Library opens on **All**, with filters **All · Notes · Documents · Files ·
Projects**. Chats, Research and Archived move to a "More" menu in the Library
header. A group card shows:

```
Tall Tree — Initial Server Setup                     Tall Tree Group › Agreements
Working master: Markdown · Current export: PDF       updated 2h ago
[Edit] [Open PDF] [History (4)]
```

Signed copies show a lock and no Edit. History and supporting files open in a
disclosure, never in the main list. The path is in a tooltip / "Copy path".
Projects shows one card per project; opening one filters the Library to it.

### Context strip

One compact strip above the chat history replaces the settings line below the
composer:

```
SDS Consulting Lead · Tall Tree Group · Editing DA-02          Change
File editing allowed · External actions require approval
```

When a document is open, the strip shows the target selector
**This document · This project · General conversation**. Opening a document
alone never changes the target or the chat mode. The composer keeps the
message box, Attach, the model picker, Agent/Chat and Send; duplicated
indicators (persona, workspace, shell, context %, dead research chip) move into
the strip or its Change panel.

### Agents

Every agent card (Agents panel and the composer Agents menu) has two buttons:
**Talk to agent** and **Assign a task**. Assign opens a small sheet: task box,
the access summary from `/access`, "Results return to this chat", and an
**Advanced** disclosure with model, workspace and tools. A blocker renders as a
sentence plus its recovery action buttons. Quick actions show as chips on the
card and in the strip when that agent answers.

### Review before export

The document editor's export menu starts with **Review changes**: changed
sections since a chosen version (default: the version the chat started from, else
the previous one), placeholders remaining (`[[…]]`, `{{…}}`, `TODO`, `TBD`,
`____`), unresolved suggestions, and **Preview PDF**. Domain checks (totals,
commercial impact) are not hard-coded; an agent may add them in its reply.

## Rules

- Move shared interactive nodes; never hide a node and add a copy (see
  `APPEARANCE.md`).
- New CSS goes in a dedicated stylesheet per area (`library.css`,
  `context-strip.css`, `agent-actions.css`, `nav-groups.css`, `doc-review.css`),
  linked from `index.html`, using existing tokens and `color-mix`. Check light
  and dark, Agamemnon and classic, 1440 / 900 / 390 px.
- No new permission is granted to make a flow work. Recovery actions come from
  the backend.

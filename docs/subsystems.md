# Subsystems

What each feature area of Odysseus is, what it owns, where its code and data
live, and what it depends on. This is a map, not a tutorial: every entry points
at the files that hold the real answer.

Where a subsystem already has a document of its own, this file summarises it in
two or three lines and links there. The linked document is the authority.

Nothing here describes the agent runtime — the agent loop, tool selection,
prompt assembly, context compaction and steering. Where a subsystem exposes
tools to the agent, this file only names them.

## Conventions shared by every subsystem

**Routes.** Each feature is one router module under `routes/`, registered in
`app.py` by an explicit `app.include_router(...)` call. Most modules export a
`setup_*_routes(...)` factory that takes its dependencies as arguments; a few
(`routes/session_routes.py`, `routes/mcp_routes.py`, `routes/upload_routes.py`,
`routes/compare/compare_routes.py`, `routes/webhook/webhook_routes.py`) define a
module-level `router` instead. Registration order matters: FastAPI matches the
first route registered for a path.

**Data root.** Every persistent path is derived from `DATA_DIR` in
`src/constants.py`, which reads `ODYSSEUS_DATA_DIR` and otherwise sits at
`<app root>/data`. `core/constants.py` is a re-export shim over that module, not
a second copy. The SQLite database is `data/app.db`; its schema and its
hand-rolled migrations are all in `core/database.py`.

**Ownership.** Most tables carry a nullable `owner` column holding a username.
`None` means "legacy or shared" and is visible to everyone;
`src/auth_helpers.owner_filter` is the shared way to scope a query.

**Frontend.** `static/` is raw ES modules with no build step. `static/app.js` is
the orchestrator; each feature is one module (or one directory) under
`static/js/`. `static/js/MODULE_SUMMARY.md` describes the layout.
`app.py`'s `_RevalidatingStatic` sends `Cache-Control: no-cache` for
`.js`/`.css`/`.html`, because there are no versioned asset URLs.

**Names that collide.** Three, and all three bite:

| Word | In this repo it usually means | But also |
|---|---|---|
| vault | the Markdown knowledge base under `data/personal_docs` | `/api/vault` plus `data/vault.json` is the **Bitwarden/Vaultwarden CLI** integration (`routes/vault/vault_routes.py`) |
| editor | the document editor (`static/js/document.js`) | `static/js/editor/` is the **gallery image** editor's internals |
| cookbook | local model download and serving | also the `/api/codex/*` `cookbook:*` token scopes, which are that feature's remote-control API |

---

## Chat and sessions

A session is one conversation: a model, an endpoint, a message list and a bag of
per-chat settings. Everything else in the app hangs off a session id.

| | |
|---|---|
| Routes | `routes/chat_routes.py` (`/api/chat`, `/api/chat_stream`, `/api/chat/resume/{id}`, `/api/chat/stop/{id}`), `routes/session_routes.py` (`/api/sessions`, `/api/session/...`), `routes/history/history_routes.py` (transcript edits, fork, compact) |
| Server | `core/session_manager.py` (`SessionManager`), `src/chat_handler.py`, `src/chat_processor.py`, `src/session_settings.py`, `src/session_search.py`, `src/session_actions.py` |
| Frontend | `static/js/chat.js`, `chatStream.js`, `chatRenderer.js`, `sessions.js`, `search-chat.js` |
| Data | `sessions`, `chat_messages`, `usage_ledger_entries` tables; FTS5 index `chat_messages_fts` (`core/database.py`, `_migrate_chat_messages_fts`) |
| Agent tools | `manage_session`, `create_session`, `list_sessions`, `send_to_session`, `search_chats` |

Per-chat state lives in `sessions.settings_json` as JSON, split by
`src/session_settings.py` into policy the server enforces (`approval_mode`,
`disabled_tools`, `private_vault_access`) and UI state the frontend restores on
reopen. New keys therefore need no migration.

`SessionManager.load_sessions` loads **metadata only** for the 100 most recent
unarchived sessions; messages are hydrated per session by `get_session`.
`SessionManager.save_sessions` is a no-op kept for compatibility, and the
`sessions.json` path is still passed to the constructor but ignored — the
database is the store.

`sessions.last_message_at` is deliberately not `onupdate`: it moves only when a
message is persisted, so renaming a chat or merely opening it does not make it
look active. `updated_at` and `last_accessed` do move.

Attachments are stored once under `data/uploads` (index `uploads.json`,
`src/upload_handler.py`) and referenced from message content by a stable
`attachment_ref` rather than inlined — see `docs/attachments.md` and
`src/attachment_refs.py`.

## Documents and the document editor

A document is a living text file the model can rewrite in place, with a version
history, opened in a tabbed panel beside the chat.

| | |
|---|---|
| Routes | `routes/document/document_routes.py` (`/api/document`, `/api/documents/library`, `/api/document/{id}/versions`, `/api/documents/import-pdf`) |
| Server | `src/document_actions.py` (reusable actions, also called by the scheduler), `src/document_processor.py` (extraction), `src/office_doc.py`, `src/pdf_forms.py`, `src/pdf_form_doc.py`, `src/markitdown_runtime.py` |
| Frontend | `static/js/document.js` (editor panel), `static/js/documentLibrary.js` (Chats / Documents / Research / Archive modal), `static/js/diffView.js` |
| Data | `documents`, `document_versions` tables |
| Agent tools | `create_document`, `update_document`, `edit_document`, `manage_documents`, `suggest_document` |

`documents.owner` exists because a document used to derive ownership from its
session, and `documents.session_id` is `ON DELETE SET NULL` — deleting a chat
orphaned its documents out of the library. Own the row.

Two soft-delete flags mean different things: `is_active` tracks "open in a
session", `archived` hides it from the library, search and Tidy.

Office and PDF attachments are auto-materialised as documents so the agent can
page through the full extracted text after the inline chat payload is capped —
`create_office_document` in `src/office_doc.py`, and `src/pdf_form_doc.py` for
fillable forms. Both depend on optional packages (`markitdown`, `pypdf`,
PyMuPDF) declared in `requirements-optional.txt`; when absent the callers
degrade rather than fail.

## Notes and the Markdown vault

Notes are Google-Keep-style notes and checklists that are stored **as Markdown
files inside the vault**, not as database rows.

| | |
|---|---|
| Routes | `routes/note/note_routes.py` (`/api/notes`); `routes/personal_routes.py` serves `/api/personal/vault/tree` and `/vault/file` for browsing and editing arbitrary vault files |
| Server | `src/notes_store.py` (`MarkdownNotesStore`, exported as `STORE`), `src/notes_markdown.py` (the note ↔ Markdown codec), `src/notes_vault_migration.py` |
| Frontend | `static/js/notes.js` (notes panel and the vault file browser) |
| Data | `.md` files under `<vault>/Notes` and `<vault>/Notes/Archive`; folder names come from the `notes_directory` and `notes_archive_directory` settings |
| Agent tools | `manage_notes` |

The vault root is `vault_root()` in `src/rag_sensitivity.py`: the
`vault_directory` setting when set, otherwise `PERSONAL_DIR`
(`data/personal_docs`). Notes inherit the vault's folder sensitivity labels by
being files in it — that was the point of the move.

The `notes` table in `core/database.py` is **legacy**. `routes/note/note_routes.py`
imports `src.notes_store.STORE` and never touches it; the only live reader is
`src/notes_vault_migration.py`, which copies rows out to `.md` at startup (from
`src/app_initializer.py`) and never deletes them. The migration is
manifest-backed with `plan` / `apply` / `rollback` verbs, runnable as
`python -m src.notes_vault_migration`.

Two codec rules to know before editing `src/notes_markdown.py`: the frontmatter
title is verbatim and is the note's identity (the filename is a sanitised
label), and a checklist's items are the trailing run of `- [ ]` lines — but the
body is only scanned for them when frontmatter says `x-note-type: checklist`.

Due-date reminders are fired by `action_ping_notes` in `src/builtin_actions.py`,
which ticks every 60s and keeps per-owner state in
`data/note_pings_<owner>.json`.

## Personal documents and RAG retrieval

Semantic search over a tree of files — an Obsidian-style Markdown vault plus
whatever else is indexed — injected into chats and reachable as a tool.

**Read `docs/vault-retrieval.md` first.** It covers extraction, chunking,
ranking, embedding lanes, the `private` label and every environment variable.
What follows is only the wiring.

| | |
|---|---|
| Routes | `routes/personal_routes.py` (`/api/personal/vault/tree`, `/scan`, `/add_directory`, `/directory_sensitivity`, `/upload`, `/remove_directory`), `routes/embedding_routes.py` (admin-only embedder config) |
| Server | `src/personal_docs.py` (`PersonalDocsManager`), `src/rag_vector.py` (`VectorRAG`), `src/rag_manager.py` (a thin wrapper over it), `src/rag_ranking.py`, `src/rag_sensitivity.py`, `src/vault_markdown.py`, `src/vault_scan.py`, `src/embedding_lanes.py`, `src/embeddings.py`, `src/chroma_client.py` |
| Frontend | `static/js/rag.js` (admin panel), `static/js/notes.js` (vault tree) |
| Data | files under `vault_root()`, default `data/personal_docs`; Chroma collections named from `odysseus_rag` plus a lane suffix; index state JSON under `data/personal_docs` |
| Agent tools | `search_documents`; also the built-in MCP server `mcp_servers/rag_server.py` |

`src/rag_sensitivity.py` is the policy layer everything else asks. It resolves a
per-path `public`/`private` label — file frontmatter beats the
`vault_folder_sensitivity` setting beats the legacy `directory_sensitivity.json`
beats a default — and `assert_vault_writable` refuses agent writes into a
readonly folder while deliberately leaving human routes alone.

Retrieval degrades to nothing, not to a fallback: with Chroma unreachable
`get_rag_manager()` returns `None` and `search_documents` says so explicitly.

## Memory

Short durable facts about the user, injected into chats and searchable by the
agent.

| | |
|---|---|
| Routes | `routes/memory/memory_routes.py` (`/api/memory/add`, `/search`, `/timeline`, `/extract`, `/audit`, `/import`) |
| Server | `src/memory.py` (`MemoryManager`), `src/memory_vector.py` (`MemoryVectorStore`), `src/memory_provider.py`, `services/memory/` |
| Frontend | `static/js/memory.js` |
| Data | `data/memory.json` (the store); Chroma collections from `odysseus_memories` plus a lane suffix |
| Agent tools | `manage_memory`; also `mcp_servers/memory_server.py` |

The store is the JSON file, not the `memories` table — `MemoryManager.__init__`
takes `data_dir` and opens `memory.json`. The table exists and is imported in
exactly one place (`src/builtin_actions.py`).

`MemoryManager.load_all_for_update` raises `MemoryStoreUnreadable` rather than
returning `[]` when the file is present but unparsable. The distinction is
load-bearing: a read-modify-write caller that treats "unknown" as "empty"
persists the empty view, and the writes are atomic, so the loss is durable.

The vector index is a convenience over the same entries and is rebuilt from them
when found empty (`src/app_initializer.py`). It shares the lane machinery in
`src/embedding_lanes.py` with RAG, so it has the same "Chroma unreachable means
no semantic recall" failure mode.

## Skills

A skill is a `SKILL.md` procedure — frontmatter plus When to Use / Procedure /
Pitfalls / Verification — that the agent can look up and follow.

| | |
|---|---|
| Routes | `routes/skills_routes.py` (`/api/skills/index`, `/add`, `/builtin`, `/slash-catalog`, `/import-from-url`) |
| Server | `services/memory/skills.py` (`SkillsManager`), `services/memory/skill_format.py` (parser and writer), `skill_extractor.py`, `skill_importer.py`, `src/builtin_skills.py`, `src/teacher_escalation.py` |
| Frontend | `static/js/skills.js` (Skills tab of the Memory modal), `static/js/slashCommands.js` |
| Data | `data/skills/<category>/<name>/SKILL.md`; usage counters in the sidecar `data/skills/_usage.json`; legacy `data/skills.json` entries surface read-only |
| Agent tools | `manage_skills` |

Skills bundled with the repo live under `skills/<category>/<name>/` and are
seeded into `data/skills` at startup by `seed_bundled_skills`
(`src/builtin_skills.py`). The seeder reconciles metadata but never replaces a
skill's body, so a user edit survives an upgrade.

`/api/skills/builtin` is not a skill listing at all — it renders the agent's
built-in tool capabilities from `agent_loop.TOOL_SECTIONS` so the UI can show
them beside learned skills.

`source` in frontmatter gates automation: skills added through the REST route
are `"user"` and are exempt from auto-dedup and cap-eviction, while the agent's
own writes are `"learned"`. The `test_skills` and `audit_skills` builtin actions
(`src/builtin_actions.py`) operate on the latter.
`src/teacher_escalation.py` is the other writer: when a self-hosted model fails
a turn in agent mode and a `teacher_model` is configured, a SOTA endpoint both
answers and writes the `SKILL.md` — and only if the teacher's own reply passes
the same failure regex.

## Deep research

A multi-round Think → Search → Extract → Synthesise loop that produces a cited
report, run as a background job so it survives a page reload.

| | |
|---|---|
| Routes | `routes/research/research_routes.py` (`/api/research/start`, `/status/{sid}`, `/report/{sid}`, `/library`, `/cancel/{sid}`) |
| Server | `src/research_handler.py` (`ResearchHandler` — the task registry), `src/deep_research.py` (the engine), `src/goal_based_extractor.py`, `src/research_utils.py`, `src/visual_report.py` (self-contained HTML report) |
| Frontend | `static/js/research/panel.js`, `static/js/research/jobs.js`, `static/js/researchSynapse.js` (live SVG of the run) |
| Data | one JSON file per session at `data/deep_research/<session_id>.json` |
| Agent tools | `trigger_research`, `manage_research` (`src/tools/research.py`) |

Depends on the search layer (`src/search/`, `services/search/`) for results and
on `src/llm_core.py` for every decision in the loop — the model chooses the
queries, the relevance and the stopping point, so one research run is many model
calls.

Orphaned report files (the session was deleted) are swept by the `tidy_research`
builtin action.

`services/research/research_handler.py` is a second, shorter copy of the handler
that nothing imports; `src/app_initializer.py` constructs the one in `src/`.

## Email

A full IMAP/SMTP client — inbox, triage, tags, AI summaries and reply drafts,
scheduled sends — over one or more configured accounts.

| | |
|---|---|
| Routes | `routes/email_routes.py` (`/api/email/...`), with helpers in `routes/email_helpers.py` and background loops in `routes/email_pollers.py` |
| Server | `src/email_thread_parser.py` (server-side port of the JS quote parser), `src/secret_storage.py` (credential encryption) |
| Frontend | `static/js/emailInbox.js` (sidebar list), `static/js/emailLibrary.js` (grid modal), `static/js/emailShared.js`, `static/js/emailLibrary/` |
| Data | `email_accounts` table; caches and queues in `data/email_cache.db` and `data/scheduled_emails.db`; attachments under `data/mail-attachments` (`ODYSSEUS_MAIL_ATTACHMENTS_DIR`) |
| Agent tools | `list_emails`, `read_email`, `send_email`, `reply_to_email`, `archive_email`, `delete_email`, `mark_email_read`, `bulk_email`, `audit_emails`, `scan_email_unsubscribes`, `unsubscribe_email`, `list_email_accounts`; also `mcp_servers/email_server.py` |

IMAP/SMTP passwords and Google OAuth tokens are Fernet-encrypted through
`src/secret_storage.py` with the key at `data/.app_key` (mode 0600). The stated
threat model is a stolen SQLite backup, not process compromise — anything that
can read `.app_key` can read every credential.

The two auxiliary SQLite files are **not** SQLAlchemy models and are not in
`app.db`: they are opened with plain `sqlite3` and their schema lives in
`CREATE TABLE IF NOT EXISTS` statements inside `routes/email_helpers.py`
(`scheduled_emails`, `email_summaries`, `email_ai_replies`, `email_tags`,
`email_message_index`, and a dozen more).

Microsoft basic auth is disabled on most tenants and there is no Graph or OAuth
path for Outlook — see `docs/email-outlook.md`.

Signatures are shared, not an email feature: `routes/signature_routes.py` and
the `signatures` table hold reusable base64-PNG image stamps used by PDF form
filling, email composition and the document editor, Fernet-encrypted at rest.

Email reaches outward into documents (an attachment becomes a document, and
`documents.source_email_*` threads the reply back onto the original
conversation), calendar (`extract_email_events`), contacts
(`routes/contacts/`), and the scheduler, which owns `summarize_emails`,
`draft_email_replies`, `email_auto_translate` and `check_email_urgency`.

## Calendar (CalDAV, Google, Todoist)

A month/week/year calendar over local events, CalDAV accounts and Todoist tasks.

| | |
|---|---|
| Routes | `routes/calendar_routes.py` (`/api/calendar/config`, `/config/accounts`, `/oauth/google/authorize`, `/oauth/google/callback`) |
| Server | `src/caldav_sync.py` (remote → local pull), `src/caldav_writeback.py` (local → remote push), `src/todoist_calendar_sync.py`, `src/oauth_errors.py`, `src/user_time.py` |
| Frontend | `static/js/calendar.js`, `static/js/calendar/reminders.js`, `static/js/calendar/utils.js` |
| Data | `calendars`, `calendar_events`, `caldav_deleted_events` tables. CalDAV **account credentials are not in the database** — they live in `data/user_prefs.json` under `caldav_accounts`, read and written by `_load_caldav_accounts` / `_save_caldav_accounts` in `src/caldav_sync.py` |
| Agent tools | `manage_calendar` (`src/tools/calendar.py`) |

Sync is two separate one-way halves. The pull maps each remote calendar to one
`CalendarCal` row whose id is a stable hash of the remote URL, and prunes
CalDAV-sourced events that vanished upstream. The push is best-effort: the local
row stays the source of truth, a failed write leaves a `caldav_sync_pending`
marker, and a failed delete leaves a tombstone in `caldav_deleted_events` so the
event is retried rather than resurrected by the next pull.

`calendar_events.origin` is what protects locally created events from that
prune — a UI event whose write-back failed is deliberately left without it.
`is_utc` distinguishes rows stored as UTC instants from legacy naive-local rows
and drives the `Z` suffix on serialisation; get it wrong and events move by the
offset.

Google calendars use CalDAV with an OAuth bearer token, not basic auth;
`is_google_caldav_url` makes a password attempt fail with an explanatory error
instead of a generic 401. Todoist remains its own source of truth and is cached
into a synthetic `todoist-<hash>` calendar; `TODOIST_API_TOKEN` enables it.

## Gallery, image generation and editing

A photo library that also holds AI-generated images, with a layered canvas
editor on top of it.

| | |
|---|---|
| Routes | `routes/gallery/gallery_routes.py` (`/api/gallery/upload`, `/library`, `/albums`, `/ai-upscale`, `/style-transfer`, `/ai-tag-batch`), `routes/editor_draft_routes.py` (`/api/editor-drafts`) |
| Server | `src/generated_images.py` (path resolution and cache headers), `src/image_model_ids.py`, `do_generate_image` and `do_edit_image` in `src/ai_interaction.py` |
| Frontend | `static/js/gallery.js`, `static/js/galleryEditor.js`, and the whole `static/js/editor/` tree (state, tools, filters, fx, AI tools) |
| Data | `gallery_images`, `gallery_albums`, `editor_drafts` tables; **all image bytes in `data/generated_images/`** |
| Agent tools | `edit_image` (`src/tools/image.py`, an HTTP loopback to `/api/gallery/{action}`), `generate_image` |

`GALLERY_DIR` and `GALLERY_UPLOADS_DIR` are defined in `src/constants.py` but the
gallery does not write to them: `routes/gallery/gallery_routes.py` sets
`GALLERY_IMAGE_DIR = Path(GENERATED_IMAGES_DIR)`, so uploads and generated
images share one directory. The only other reference to those two constants is
the admin wipe route.

Image generation is a route-level branch, not a tool.
`_is_image_generation_session` in `routes/chat_routes.py` sends the whole turn
down the image path when the model name looks like an image model
(`src/image_model_ids.py`) or when the session's endpoint is an enabled
`model_type == "image"` endpoint whose model cache contains the selection. The
endpoint check is deliberately narrow so an image endpoint on the same host
cannot capture ordinary text models.

`/api/generated-image/{filename}` is auth-gated and checks ownership against the
gallery row; a generated file that has no row yet is allowed through.

`editor_drafts` belongs to the **gallery** editor (layer pixels as base64 PNG
data URLs, offsets, opacities, plus a thumbnail), not to the document editor.

## Cookbook (local model download, serve, hardware fit)

Pick a model the hardware can actually run, download it, and serve it — locally
or over SSH on another box — then wire the result into the model picker.

| | |
|---|---|
| Routes | `routes/cookbook_routes.py` (`/api/model/download`, `/api/model/serve`, `/api/cookbook/*`), helpers in `routes/cookbook_helpers.py` and `routes/cookbook_output.py`; `routes/hwfit_routes.py` (`/api/hwfit/system`, `/models`, `/profiles`, `/image-models`); `routes/shell_routes.py` (package install, engine rebuild) |
| Server | `services/hwfit/` (`fit.py`, `hardware.py`, `models.py`, `profiles.py`, `hf_discovery.py`, plus `data/hf_models.json`), `src/tools/cookbook.py`, `src/cookbook_serve_lifecycle.py`, `src/host_docker_access.py` |
| Frontend | `static/js/cookbook.js` plus `cookbook-hwfit.js`, `cookbookDownload.js`, `cookbookServe.js`, `cookbookRunning.js`, `cookbookPorts.js`, `cookbookSchedule.js`, `cookbook-diagnosis.js`, `cookbook-deps-recipes.js` |
| Data | `data/cookbook_state.json` (hosts, tasks, tokens — secrets encrypted via `src/secret_storage.py` and stripped from API responses by `_strip_task_secrets`); tmux logs under the system temp dir (`TMUX_LOG_DIR` in `routes/shell_routes.py`) |
| Agent tools | `download_model`, `serve_model`, `stop_served_model`, `list_served_models`, `list_cached_models`, `list_downloads`, `cancel_download`, `search_hf_models`, `adopt_served_model`, `list_cookbook_servers`, `list_serve_presets`, `serve_preset`, `tail_serve_output` |

A serve is a **tmux session running a generated shell script** (a PowerShell
background process on Windows), started locally or over SSH. It is not a managed
subprocess: stopping Odysseus does not stop the model server, and output is read
by tailing the tmux log rather than from a pipe.

Serving writes a `ModelEndpoint` row so the model appears in the picker
(`_ensure_served_endpoint` in `src/tools/cookbook.py`). The scheduler's
`cookbook_serve` action stamps its serves with `_scheduledStopAtMs`, and
`src/cookbook_serve_lifecycle.py` ticks every 60s to kill an expired one *and*
delete the endpoint it registered — without that second half the picker keeps
offering a dead endpoint.

Hardware fit is pure computation in `services/hwfit`: estimate memory from
parameter count and quantisation, rank against detected or manually simulated
hardware. `_apply_manual_hardware` in `routes/hwfit_routes.py` **replaces**
detected hardware rather than adding to it — it is a "what if I had this box"
simulator, and the accepted backends are a fixed subset of what
`services/hwfit/fit.py` understands.

## Compare

Run one prompt against two model/endpoint pairs side by side, optionally blind,
and record which won.

| | |
|---|---|
| Routes | `routes/compare/compare_routes.py` (`/api/compare/start`, `/{id}/vote`, `/record`, `/history`) |
| Frontend | `static/js/compare/` — `index.js`, `panes.js`, `stream.js`, `selector.js`, `vote.js`, `scoreboard.js`, `probe.js` |
| Data | `comparisons` table (both responses, per-side metrics JSON, `winner`, `is_blind`, `blind_mapping`) |

`/start` creates ephemeral sessions named `[CMP] …`, one per side, and the
frontend then drives ordinary `/api/chat_stream` calls against them. That is why
comparison is not a separate execution path: it reuses chat entirely, and a
comparison run shows up as sessions.

The endpoint lookup is owner-scoped on purpose. `_owned_endpoint_by_url` matches
only a `ModelEndpoint` visible to the caller, because `/start` copies the matched
row's decrypted `api_key` into the `[CMP]` session's headers — an unscoped match
would let one user spend another user's key against a base URL of their choosing.

Blind mode keeps the left/right → A/B mapping in `blind_mapping`, so the reveal
after voting is server-side truth rather than something the client remembers.

## Tasks and the scheduler

Recurring or one-off work: either an LLM prompt run as an agent turn, or a named
built-in action that needs no model at all.

| | |
|---|---|
| Routes | `routes/task_routes.py` (`/api/tasks/...`, including `/{task_id}/webhook/{token}`), `routes/assistant_routes.py` (`/api/assistant`) |
| Server | `src/task_scheduler.py`, `src/builtin_actions.py` (the action registry), `src/task_action_policy.py`, `src/task_endpoint.py`, `src/interactive_gate.py`, `src/bg_monitor.py` |
| Frontend | `static/js/tasks.js`, `static/js/assistant.js` |
| Data | `scheduled_tasks`, `task_runs`, `crew_members` tables |
| Agent tools | `manage_tasks` |

`task_type` selects the engine. `"llm"` runs the agent against `prompt`;
`"action"` looks `action` up in `BUILTIN_ACTIONS` in `src/builtin_actions.py`
and calls a plain Python function (tidy-ups, email triage, calendar extraction,
`ssh_command`, `run_script`, `cookbook_serve`, the skill test/audit pair). Two
actions in that module are deliberately **not** in the registry —
`action_ping_events` and `action_ping_notes` — because they run only from their
own loops.

An action raises `TaskNoop` (a `BaseException`, so the `except Exception`
handlers around real errors cannot swallow it) to say "nothing to do": the
scheduler drops the queued `TaskRun`, advances `last_run`/`next_run`, and logs
nothing to the activity feed.

Triggering is either time (`schedule` plus `scheduled_time`/`scheduled_day`, or
`cron_expression`) or an event (`trigger_type="event"` with `trigger_event` and
`trigger_count`). `then_task_id` chains one task onto another, and
`webhook_token` makes a task callable from outside with no session cookie — the
path is the credential, which is why `/api/tasks/{id}/webhook/{token}` is in
`AUTH_EXEMPT_PATTERNS` in `app.py`.

`src/interactive_gate.py` holds background work until UI traffic has settled, so
scheduled jobs do not compete with the user opening a panel.

The personal assistant is not a separate subsystem: it is a `CrewMember` with
`is_default_assistant=True` owning one pinned session and three daily check-in
`ScheduledTask` rows (`routes/assistant_routes.py`).

## Workbench and agent worktrees

Two things that sit next to each other: a window showing what every agent is
doing, and a gated flow for letting an agent push code.

**`docs/workbench.md`** covers the Workbench UI (Activity, Changes, Commits,
PRs). **`docs/agent-worktree.md`** covers the publish flow and its host CLI.
Read those; the wiring is below.

| | |
|---|---|
| Routes | `routes/workbench_routes.py` (`/api/workbench/activity`, `/activity/stream`, `/runs`, `/repo/*`, `/prs/*`), `routes/agents_routes.py` (`/api/agents`), `routes/claude_code_routes.py` (`/api/claude-code/tasks`), `routes/workspace_routes.py` (`/api/workspace/browse`) |
| Server | `src/agent_activity.py` (the one feed everything reports into), `src/repo_inspect.py` (read-only git), `src/agent_worktree/` (`config.py`, `approval.py`, `push_guard.py`, `sensitive.py`, `github.py`, `service.py`, `diagnostics.py`) |
| Frontend | `static/js/workbench.js`, `static/js/agentsDashboard.js`, `static/js/agentThread.js`, `static/js/diffView.js` |
| Data | one JSONL file per session under `data/agent_activity/`; `data/claude_code_tasks.json`; `data/bg_jobs.json` |
| Agent tools | `manage_agent_worktree`, `delegate_to_claude_code`, `manage_agent_loadout`, `message_agent` |

Everything under `/api/workbench` is admin-only for cookie sessions — its
`_admin()` helper calls both `require_user` and `require_admin` — because it
exposes host checkouts and can write to GitHub. `/api/workspace/browse` is gated
the same way, since enumerating the server filesystem is the same capability as
the file tools.

`src/agent_worktree/config.py` is fail-closed: a missing or malformed value
counts as "not configured" and blocks publishing, and it re-reads on every call
rather than caching at import. `BRANCH_PREFIX = "agent/odysseus/"` is a module
constant and deliberately not an environment variable, so a mis-set variable can
never widen the agent's push reach to `main`. Approval TTLs are clamped, not
trusted.

`src/agent_activity.py` states that it is **not** a security boundary — it
stores what callers give it, clamped, and the routes that expose the feed apply
the admin gate.

## Lotus (wellbeing)

Private daily mood check-ins with descriptive summaries and reminders, backed by
a vendored MCP project rather than by Odysseus's own database.

| | |
|---|---|
| Routes | `routes/lotus_routes.py` (`/api/lotus/overview`, `/checkins`, `/preferences`, `/access-policy`) |
| Server | `src/lotus_checkins.py` (`LotusCheckinStore`), `src/lotus_insights.py`, `src/lotus_access.py`, `src/lotus_notifications.py`, `src/reminder_personas.py` |
| Frontend | `static/js/lotus.js` |
| Data | per-owner SQLite at `<lotus data root>/users/<owner key>/mood.db`, plus `lotus_preferences` and `lotus_notifications` tables Odysseus creates in the same file |
| Agent tools | `manage_wellbeing` (`src/tools/wellbeing.py`); also the bundled `mcp_servers/lotus_server.py` |

Schema, models and query services are imported at runtime from the vendored
`lotus-mcp/src` tree, which `src/lotus_checkins.py` pushes onto `sys.path`.

**Lotus does not follow `ODYSSEUS_DATA_DIR`.** `_lotus_data_root()` reads
`LOTUS_DATA_DIR` and otherwise uses `<app root>/data/lotus/data`; the MCP server
reads `LOTUS_ROOT` and defaults to `<app root>/data/lotus`. On an install that
moved its data directory, Lotus data stays behind.

Model access is a per-owner policy, not a global switch. `src/lotus_access.py`
defaults to allowing `local` and `lan` endpoints and denying `api`, classified by
`classify_endpoint_scope` in `src/model_context.py`; a denied endpoint has every
Lotus MCP tool hidden and runtime-blocked (`src/mcp_manager.py`).

`src/lotus_insights.py` never reads the `note` column — its statistics come from
`summary_service`, whose queries do not select notes at all. That is a stated
invariant, not an accident.

## MCP servers and integrations

Two adjacent things: MCP tool servers the agent can call, and simple HTTP
service connections (RSS, bookmarks, home automation, push).

| | |
|---|---|
| Routes | `routes/mcp_routes.py` (`/api/mcp/servers`, `/tools`, OAuth), `routes/webhook/webhook_routes.py` (`/api/webhooks`, plus `/api/v1/chat`), `routes/vault/vault_routes.py` (`/api/vault` — Bitwarden CLI), `routes/codex_routes.py` (`/api/codex`, `/api/claude`) |
| Server | `src/mcp_manager.py` (`McpManager`), `src/builtin_mcp.py` (auto-registration), `src/mcp_oauth.py`, `src/integrations.py`, `src/webhook_manager.py` |
| Frontend | `static/js/admin.js` (MCP and integrations panels) |
| Data | `mcp_servers` table (OAuth tokens in an `EncryptedText` column), `webhooks` table, `data/integrations.json`, `data/mcp_oauth/`, `data/vault.json` |
| Agent tools | `manage_mcp`, `manage_webhooks`, plus every tool each connected server exposes |

Built-in servers are registered at startup by `register_builtin_servers` in
`src/builtin_mcp.py`: Python stdio children under `mcp_servers/` (`image_gen`,
`memory`, `rag`, `email`, `todoist`, `lotus`, and `pi_worker` when
`ODYSSEUS_PI_WORKER_HOST` is set), an npx-launched Playwright browser server,
and the GitHub MCP Go binary baked into the image. GitHub is split into a
read-only server and an opt-in `ODYSSEUS_GITHUB_MCP_WRITE=1` write server, both
pinned to explicit `--tools` lists with no repository-mutating tools and no
open-ended `--toolsets`. A malformed `GITHUB_PERSONAL_ACCESS_TOKEN` no longer
registers a misleading server that can only return 401. `ODYSSEUS_DISABLE_MCP=1`
turns the whole layer off.

`src/integrations.py` is a different mechanism entirely: named presets
(`miniflux`, `gitea`, `linkding`, `homeassistant`, `ntfy`, `discord_webhook`,
`vaultwarden`, `freshrss`) stored in `data/integrations.json` with credentials
encrypted through `src/secret_storage.py`. The `integrations` table exists in
the schema, but the JSON file is what this module reads and writes.

`src/webhook_manager.py` is **outgoing** — HMAC-SHA256-signed POSTs fired on
events. The inbound directions are `/api/v1/chat` under an API token, and the
per-task webhook URLs owned by the Tasks subsystem.

`/api/codex` and `/api/claude` are the reverse of delegation: HTTP surfaces an
external Codex or Claude Code session calls *back into*, authorised purely by
API-token scope for token callers and by admin for cookie callers.

## Auth, users and API tokens

Who is making the request, and what they are allowed to do.

| | |
|---|---|
| Routes | `routes/auth_routes.py` (`/api/auth/setup`, `/login`, `/2fa/setup`, `/status`, `/policy`), `routes/api_token_routes.py` (`/api/tokens`) |
| Server | `core/auth.py` (`AuthManager`), `core/middleware.py` (`require_admin`, `SecurityHeadersMiddleware`), `src/auth_helpers.py` (`get_current_user`, `require_user`, `require_privilege`, `owner_filter`), `src/rate_limiter.py`, `src/secret_storage.py`. The `AuthMiddleware` class itself is defined inline in `app.py` |
| Frontend | `static/login.html`, `static/js/admin.js` (users and tokens) |
| Data | `data/auth.json` (users, bcrypt hashes, TOTP secrets, per-user privileges), `api_tokens` table (bcrypt `token_hash` plus an 8-character `token_prefix` for display) |
| Agent tools | `manage_tokens` |

Auth exists only when `AUTH_ENABLED` is true; otherwise no middleware is added
and `owner` is `None` everywhere, which is also what the `owner`-nullable columns
mean. `load_dotenv(encoding="utf-8-sig")` in `app.py` is there because a BOM
written by Notepad made the first `.env` key unparsable, silently forcing login
on.

`AuthMiddleware.dispatch` tries, in order: genuine CORS preflight, the exempt
path list, the in-process internal-tool header (loopback clients only, with
optional `X-Odysseus-Owner` attribution), a direct-localhost bypass, a bearer
API token, then the session cookie. Token verification uses an in-memory cache
keyed by token prefix and invalidated on create/revoke, because bcrypt-checking
every active token on every request did not scale.

Reserved usernames in `core/auth.py` matter: `require_admin` grants admin to any
request whose user is `internal-tool`, so a real account with that name would be
a privilege escalation. Privileges are a per-user dict (`DEFAULT_PRIVILEGES`),
and `block_all_models` is a separate sentinel because an empty `allowed_models`
list already means "no restriction".

API-token scopes are a fixed allowlist in `routes/api_token_routes.py`
(`chat`, `todos:*`, `documents:*`, `email:*`, `calendar:*`, `memory:*`,
`cookbook:*`, `claude_code:*`, `vault:read`, `vault:read_private`), grouped into
named profiles. `vault:read_private` is deliberately separate so a default agent
token cannot ship journal text to a hosted provider.

See `docs/security-ci.md` for the scanning and CI side, and
`docs/configuration.md` for which knobs belong in `.env` rather than Settings.

## Backup and restore

Two different things with confusingly similar names.

**`scripts/odysseus-backup`** snapshots the whole `data/` tree into a gzip
tarball and restores it, copying SQLite files through SQLite's own `.backup` API
so a running app cannot corrupt the snapshot. It is documented in
`docs/backup-restore.md` — read that, including its warning that a snapshot
contains `data/.app_key` and therefore every stored credential.

**`routes/backup_routes.py`** (`GET /api/export`, `POST /api/import`) is a much
smaller, admin-only JSON export of *user-level* content: memories, presets,
skills, `settings.json`, `features.json`, and the caller's entry in
`user_prefs.json`. It does not include chats, documents, the vault, the gallery,
email or the database.

| | |
|---|---|
| Routes | `routes/backup_routes.py`, `routes/admin_wipe/admin_wipe_routes.py` (`DELETE /api/admin/wipe/{kind}`), `routes/cleanup/cleanup_routes.py` |
| Server | `src/cleanup_service.py`, `core/atomic_io.py` |
| Data | reads and writes the stores owned by the subsystems above |

`scripts/odysseus-backup` resolves its data directory as `<repo root>/data` and
does not read `ODYSSEUS_DATA_DIR`, so it does not follow a relocated data
directory.

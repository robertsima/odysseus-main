# Architecture and runtime

How Odysseus is assembled and what actually happens when a request arrives.
This is the machine underneath the features, not the features themselves —
for those, see the subsystem reference.

The short version: one Python process holds everything. The web server, the
scheduler, the agent runs, every manager singleton and every in-memory cache
live in a single uvicorn worker. Chroma and SearXNG are the only pieces that
are genuinely separate services, and MCP servers are child processes of that
one process. Almost every durability and multi-user property in this document
follows from that fact.

For the module-level inventory — file sizes, importer counts, refactor
candidates — see [`specs/architecture-runtime-inventory.md`](../specs/architecture-runtime-inventory.md).
That spec is the structural baseline; this document is about behaviour at
runtime.

---

## 1. What runs

### The application process

`app.py` builds the FastAPI app **at import time**. Not in a factory, not in
the lifespan hook — at module scope. Importing `app` runs
`initialize_managers()` (`src/app_initializer.py`), which constructs
`SessionManager`, `MemoryManager`, `SkillsManager`, `UploadHandler`,
`PersonalDocsManager`, `APIKeyManager`, `PresetManager`, `ChatProcessor`,
`ChatHandler`, `ResearchHandler` and `ModelDiscovery`, and then executes
~50 `app.include_router(setup_x_routes(...))` calls. Each `setup_*_routes`
function is a router factory that closes over the singletons it needs, which
is how handlers reach the managers without a DI container.

Those singletons are then parked in two places at once: module globals in
`app.py` and attributes on `app.state` (`app.state.session_manager`,
`app.state.upload_handler`, `app.state.auth_manager`, …). Some are also
registered into module-level globals elsewhere —
`core.models.set_session_manager_instance`, `src.ai_interaction`'s setters,
`src.event_bus.set_task_scheduler`. The consequence is the single-process
assumption in its purest form: there is exactly one `SessionManager` per
interpreter, and code reaches it through a process global.

`_startup_event` (wired via `app.router.lifespan_context = _lifespan`) then
starts the long-lived asyncio tasks: the background-job monitor
(`src/bg_monitor.py`), the vault scanner (`src/vault_scan.py`), MCP
connection setup, the task scheduler, an hourly null-owner sweep, the nightly
skill audit and the cookbook serve-lifecycle loop. Strong references to these
are kept in `app.state._startup_tasks` so the garbage collector cannot eat a
fire-and-forget task.

### In-process, subprocess, separate container

| Component | Where it runs | Notes |
|---|---|---|
| FastAPI app + routers | in-process | `app.py`, uvicorn, one worker |
| Task scheduler | in-process, asyncio | `src/task_scheduler.py`; gated by `ODYSSEUS_INPROCESS_TASKS` |
| Email pollers | in-process, asyncio | `routes/email_pollers.py`; gated by `ODYSSEUS_INPROCESS_POLLERS` |
| Agent loop | in-process, asyncio | `src/agent_loop.py` — runs an agent turn; documented separately |
| Detached agent runs | in-process, asyncio | `src/agent_runs.py` replay buffers |
| MCP servers | child processes (stdio) or remote HTTP/SSE | `src/mcp_manager.py`, `src/builtin_mcp.py` |
| Background bash jobs (`#!bg`) | detached OS processes | `src/bg_jobs.py`, status via on-disk exit-code files |
| ChromaDB | separate container (Docker) or separate local process (native) | HTTP only; `src/chroma_client.py` |
| SearXNG | separate container | HTTP only; `SEARXNG_INSTANCE` |
| ntfy | separate container | optional notification sink |

MCP is the one place Odysseus spawns and supervises long-lived children.
`src/builtin_mcp.py` registers built-in servers from `mcp_servers/` —
`image_gen`, `memory`, `rag`, `email`, `todoist`, `lotus`, `pi_worker` — each
launched as `sys.executable <script>` over stdio. Two more shapes exist beside
them: `npx`-launched servers (the Playwright browser server) and native
binaries (`github-mcp-server`, baked into the image). The comment in
`src/builtin_mcp.py` explains why GitHub's MCP runs as a child process rather
than a sibling container: the container image would need the host's Docker
socket mounted, which hands the Docker host to anything the model can reach.

Transports are stdio, SSE and Streamable HTTP (`McpManager.connect_server`).
Only stdio implies a child process; the other two are outbound connections.

### Docker Compose

`docker-compose.yml` is the base: services `odysseus`, `chromadb`, `searxng`,
`ntfy`. Ports bind to `127.0.0.1` by default (`${APP_BIND:-127.0.0.1}`), and
the app container's `CMD` is `uvicorn app:app --host 0.0.0.0 --port 7000` —
**no `--workers` flag**, so one process. `/app/data` and `/app/logs` are bind
mounts; Chroma and SearXNG use named volumes.

The variants differ only at the edges:

| File | Difference from the base |
|---|---|
| `docker-compose.gpu-nvidia.yml` | Self-contained copy of the base plus `docker/gpu.nvidia.yml`'s `deploy.resources.reservations.devices` and `NVIDIA_*` env, for UIs that accept only one Compose file |
| `docker-compose.gpu-amd.yml` | Same shape for ROCm |
| `docker-compose.zimaos-local.yml` | Local dry-run of the ZimaOS/CasaOS deployment: bind mounts instead of named volumes, `/DATA/...`-shaped paths, builds from the checkout instead of pulling GHCR |
| `docker/host-docker.yml` | Overlay that mounts `/var/run/docker.sock` and sets `ODYSSEUS_ENABLE_HOST_DOCKER=true`. High-trust; opt in deliberately |

SearXNG's image tag is pinned rather than `latest`, because `odysseus`
`depends_on` its healthcheck and a broken upstream tag would block the app
from starting at all.

### Native launchers

`start-macos.sh` provisions a venv, runs `setup.py`, starts a **local
ChromaDB process** from the venv's `chroma` CLI (`chroma run --path
./data/chroma`) unless `CHROMADB_HOST` points elsewhere, then execs
`uvicorn app:app`. It kills the Chroma child on exit via a `trap`.
`launch-windows.ps1` is the same idea minus Chroma. Note what neither starts:
**SearXNG**. On a native run there is no local SearXNG container, so
`SEARXNG_INSTANCE` points at nothing unless the operator supplies one.

The default port differs too: `7000` in Docker, `7860` in `start-macos.sh`
(macOS AirPlay Receiver holds 7000).

`build-macos-app.sh` does not bundle anything — it writes an `.app` wrapper
whose executable shells out to the venv's `uvicorn` in an existing checkout.

### The frozen build

`Odysseus.spec` / `build-windows-portable.ps1` run PyInstaller over
`launcher.py`, not `app.py`. `launcher.py` shows a tkinter splash, starts a
`pystray` tray icon and a browser-opener thread, then `uvicorn.run(app, ...)`
in the main thread — the same app object, wrapped in desktop affordances.

The bundled `datas` are `static`, `scripts`, `mcp_servers`,
`services/hwfit/data`, `config` and `.env.example`. `data/` is deliberately
absent: `src/runtime_paths.get_default_data_dir()` returns
`~/.odysseus/data` when `sys.frozen` is set, so the database is not written
into PyInstaller's ephemeral extraction directory. `get_app_root()` likewise
returns `sys._MEIPASS` when frozen.

The portable build has no Chroma and no SearXNG, so retrieval and web search
degrade rather than work.

### Where the single-writer assumptions are

- **One uvicorn worker.** Nothing in the Dockerfile, the launchers or
  `app.py`'s `__main__` block passes `--workers`. The logging setup in
  `app.py` says so explicitly: `RotatingFileHandler` is not multi-process
  safe, and "Odysseus is single-process by convention".
- **Process-global singletons.** `SessionManager.sessions` is an in-memory
  dict of hydrated sessions; `agent_runs._RUNS` holds live streams;
  `chat_routes._active_streams` tracks in-flight saves. A second worker would
  see none of the first worker's state.
- **`data/app.db` has one writer.** The app process owns it. The email MCP
  subprocess opens it directly with `sqlite3` (`mcp_servers/email_server.py`,
  `_read_accounts_from_db`) but only reads.
- **`data/scheduled_emails.db` has two.** Both `routes/email_helpers.py`
  (app process) and `mcp_servers/email_server.py` (MCP child) insert into it.
  That boundary is handled deliberately: the send path claims a row with
  `UPDATE scheduled_emails SET status='sending' WHERE id=? AND
  status='pending'` and only proceeds if `rowcount == 1`, so a racing poller
  cannot double-send.
- **The external-driver escape hatches.** `ODYSSEUS_INPROCESS_TASKS=0` and
  `ODYSSEUS_INPROCESS_POLLERS=0` turn off the in-process loops so cron or a
  systemd timer can drive the same work from outside. These exist precisely
  because the in-process loops assume they are the only ones running.

---

## 2. A request, end to end

### The middleware stack

Seven middlewares are registered in `app.py`. Starlette wraps them in reverse
registration order, so the last one added is the outermost:

```
  browser
    │
    ▼
  AuthMiddleware              (app.py)          outermost
    │   cookie / bearer / internal-tool / loopback → request.state.current_user
    ▼
  _SlowRequestLogMiddleware   (app.py)
    │   records route latency, warns over ODYSSEUS_SLOW_REQUEST_LOG_SECONDS
    ▼
  _InteractiveActivityMiddleware (app.py)
    │   marks the request as foreground; cancels running background tasks
    ▼
  _RequestTimeoutMiddleware   (app.py)
    │   asyncio.wait_for(..., REQUEST_HARD_TIMEOUT) unless path is exempt
    ▼
  SecurityHeadersMiddleware   (core/middleware.py)
    │   mints request.state.csp_nonce, sets CSP / XFO / HSTS on the way out
    ▼
  GZipMiddleware              (starlette)
    │   bodies ≥1024 bytes; skips text/event-stream
    ▼
  CORSMiddleware              (starlette)        innermost
    │
    ▼
  router → handler
```

Auth being outermost is why `is_cors_preflight()` exists in
`core/middleware.py`: a genuine preflight (`OPTIONS` plus
`Access-Control-Request-Method`) carries no credentials, so AuthMiddleware
has to let it fall through to the CORS middleware that can actually answer it.

**What each layer may do.** The middlewares inspect the request and may
short-circuit it; none of them touch the database except `AuthMiddleware`'s
API-token cache, and that goes through `asyncio.to_thread`.
`SecurityHeadersMiddleware` is pure header work plus the nonce.
`_SlowRequestLogMiddleware` records into `src/route_latency.py` and logs.
`_InteractiveActivityMiddleware` is the one that reaches into another
subsystem: it fires `task_scheduler.stop_background_tasks_for_foreground()`
as a detached task and wraps the downstream call in
`track_interactive_request()`.

### Authentication

`AuthMiddleware` (defined inside the `if AUTH_ENABLED:` block in `app.py`)
resolves identity in a fixed order:

1. CORS preflight → pass.
2. Exempt path → pass. The exempt set is `AUTH_EXEMPT_EXACT` (auth endpoints,
   `/api/health`, `/api/version`, `/login`), the `/static` prefix, and a
   regex for `/api/tasks/{id}/webhook/{token}` where the path itself is the
   credential and `routes/task_routes.py` validates it.
3. Internal-tool token — `X-Odysseus-Internal-Token` matching
   `core.middleware.INTERNAL_TOOL_TOKEN`, **and** `_is_trusted_loopback()`.
   This is how the agent's tool layer calls admin-gated routes over HTTP
   loopback without an admin cookie. `X-Odysseus-Owner` may re-attribute the
   request to a real user for ownership purposes only.
4. `LOCALHOST_BYPASS` plus trusted loopback.
5. `Bearer ody_…` API token, checked against an in-memory prefix→candidates
   cache and bcrypt. `last_used_at` is updated fire-and-forget so the request
   does not wait on a commit.
6. Session cookie, validated by `AuthManager`.

`_is_trusted_loopback()` is the interesting one. A bare
`client.host in ('127.0.0.1','::1')` check is unsafe behind cloudflared or a
reverse proxy, because those connect *from* loopback. So the function also
rejects any request carrying `cf-connecting-ip`, `x-forwarded-for`,
`forwarded` and friends.

Handlers then read `src.auth_helpers.effective_user(request)`, which maps a
bearer token back to the human who minted it, so an API client sees the same
data as that user's browser rather than an `api`-owned silo.

### Rate limiting

The shared limiter, `src/rate_limiter.py` (sliding window, keyed by IP),
is used in exactly one place: `routes/auth_routes.py` instantiates it three
times for login (15/60s), signup (3/300s) and first-time setup (3/300s).
Uploads carry their own separate IP limiter inside `src/upload_handler.py`
(`upload_rate_limit`, 60 uploads per minute per IP). **There is no general
API rate limit.** What protects the chat endpoints instead is
`_enforce_chat_privileges()` in `routes/chat_helpers.py`, a per-user gate on
`allowed_models` plus `max_messages_per_day`, which raises 403 or 429 before
any LLM work starts. It is applied by both `/api/chat` and `/api/chat_stream`
so neither can be used to bypass the other.

### A chat request

`POST /api/chat_stream` (`routes/chat_routes.py`, inside `setup_chat_routes`)
is the main path. Roughly:

1. Parse the body — form data, with a JSON fallback, because API callers send
   JSON and the browser sends `FormData`.
2. `_set_user_time_from_request()` reads the `X-TZ-Name` / `X-TZ-Offset`
   headers so the model gets the user's clock.
3. `_verify_session_owner(request, session)` (`routes/session_routes.py`) —
   without it any authenticated user could post into another user's chat.
4. `session_manager.get_session(session)` hydrates the session; endpoint and
   model are reconciled and repaired (`_clear_orphaned_session_endpoint`,
   `_recover_empty_session_model`).
5. `_enforce_chat_privileges()`.
6. `build_chat_context()` (`routes/chat_helpers.py`) assembles the message
   list: preset, attachment preprocessing, retrieval, memory preface, budget.
7. Dispatch by mode. `chat` mode calls `stream_llm_with_fallback()`
   (`src/llm_core.py`) directly — no tools, no document access. `agent` mode
   hands off to `stream_agent_loop()` (`src/agent_loop.py`), which is
   documented separately. `research` mode runs the research handler as a
   background task so it survives a page refresh.
8. `stream_with_save()` yields SSE frames and, on completion or client
   disconnect, persists the assistant message.
9. The run is registered with `src.agent_runs` and the response streams from
   `agent_runs.subscribe(session)`.

### Streaming versus plain JSON

A plain JSON handler returns a value; FastAPI serialises it, GZip may compress
it, `SecurityHeadersMiddleware` stamps headers on it, done. The whole thing
happens inside `_RequestTimeoutMiddleware`'s 45-second budget.

A streaming handler returns a `StreamingResponse` with
`media_type="text/event-stream"`, and four things change:

- **The timeout does not apply.** `_TIMEOUT_EXEMPT_PREFIXES` in `app.py`
  lists `/api/chat`, `/api/shell/stream`, `/api/research`,
  `/api/model/probe` and others precisely because they legitimately stay open
  for minutes.
- **Compression does not apply.** Starlette's `GZipMiddleware` skips
  `text/event-stream`, so SSE frames are never buffered into a compression
  window.
- **The response outlives the handler.** The handler returns as soon as the
  generator is constructed; the body is produced afterwards, which means
  exceptions raised mid-stream cannot become HTTP status codes. An exception
  escaping the generator becomes an `event: error` SSE frame followed by
  `[DONE]`, emitted by `agent_runs._drain`.
- **The run is detached from the connection.** `agent_runs.start()` drains
  the generator into a per-session replay buffer on a background task;
  the SSE response is only a *subscriber*. Closing the tab drops the
  subscriber, not the run. `GET /api/chat/resume/{session_id}` re-subscribes
  and replays the buffer from the beginning, then continues live. The one
  exception is compare mode, which streams `_safe_stream()` directly so that
  hitting Stop in a pane actually cancels the upstream call.

Frames are plain `data: {json}\n\n` lines terminated by `data: [DONE]`.
The `type` field carries the event kind — `attachments`, `rag_sources`,
`web_sources`, `memories_used`, `tool_start`, `tool_progress`,
`tool_output`, `doc_update`, `generated_image`, `model_info`, `compacted`,
`context_trimmed`, `research_progress`, `research_sources`,
`research_findings`, `research_done`, `message_saved`, `metrics`,
`workspace_rejected`.

If the client disconnects mid-stream, the `except (asyncio.CancelledError,
GeneratorExit)` block in `stream_with_save()` saves whatever prose has
accumulated with `stopped: True` in its metadata, so a killed stream leaves a
partial message rather than nothing.

`agent_runs` durability is explicitly in-memory: a finished run's buffer is
evicted 180 seconds after its last subscriber leaves, and **nothing survives a
process restart**.

---

## 3. Module layout and layering

There is no enforced layering rule in this repository — no import linter, no
architecture test, nothing in `CONTRIBUTING.md` beyond the path-constants
rule. What follows is the *de facto* layering, measured from the import graph
rather than asserted.

### What each directory is for

| Directory | Contains | Example |
|---|---|---|
| `core/` | Persistence and request-level primitives: SQLAlchemy models and engine, auth manager, security middleware, session manager, atomic file writes | `core/database.py`, `core/auth.py`, `core/middleware.py` |
| `src/` | Everything domain: the agent loop, tools, scheduler, retrieval, settings, integrations, managers. 137 flat modules plus seven packages — `agent_tools/`, `agent_worktree/`, `delegation/`, `model_capability_readers/`, `search/`, `subscription/`, `tools/` | `src/agent_loop.py`, `src/task_scheduler.py` |
| `routes/` | HTTP handlers. Each module exposes a `setup_*_routes(...)` factory returning an `APIRouter` | `routes/chat_routes.py` |
| `services/` | Self-contained capability packages with an async interface, designed so they could run in-process or standalone | `services/search/`, `services/memory/`, `services/tts/` |
| `mcp_servers/` | Standalone MCP server scripts, run as child processes | `mcp_servers/email_server.py` |
| `static/js/` | The frontend, raw ES modules, no build step | `static/js/chat.js` |

### The measured graph

Top-level import lines between packages, counted over `*.py`:

| Direction | Lines | Reading |
|---|---|---|
| `routes/` → `src/` | 465 | Expected — handlers calling domain logic |
| `routes/` → `core/` | 144 | Expected — handlers touching models |
| `src/` → `core/` | 133 | Expected |
| `src/` → `routes/` | 39 | **Inverted** |
| `routes/` → `services/` | 25 | Expected |
| `src/` → `services/` | 18 | Expected |
| `core/` → `src/` | 15 | **Inverted** |
| `services/` → `src/` | 36 | Blurred — `services` is not a leaf |
| `services/` → `core/` | 6 | Blurred |
| `services/` → `routes/` | 1 | **Inverted** |

Recompute with, for example:

```sh
grep -rhE '^[[:space:]]*(from|import)[[:space:]]+routes\.' --include='*.py' src/ | wc -l
```

The intended direction is `routes → src → core`, with `services` as a leaf
under both. Three families of edge break it.

### Violation 1: `src/` reaches into `routes/`

Thirty-nine import lines across sixteen files. All but one are *inline*
imports inside function bodies, which is what keeps them from becoming import
cycles — and which is also the tell. Concretely:

```python
# src/task_scheduler.py, _deliver_via_email
from routes.email_routes import _resolve_send_config
from routes.email_helpers import _send_smtp_message

# src/task_scheduler.py, ensure_defaults
from routes.prefs_routes import _load_for_user

# src/builtin_actions.py
from routes.email_pollers import _run_auto_summarize_once
from routes.skills_routes import _run_skill_test_once, _skill_test_task
from routes.note_routes import dispatch_reminder
```

Note the leading underscores. The scheduler does not merely depend on the HTTP
layer; it depends on the HTTP layer's *private* helpers. Sending an email,
loading a user's preferences and dispatching a reminder are domain
capabilities that happen to live in route modules because that is where they
were first written. Every one of these is a function that wants to be in
`src/` or `services/`.

The one top-level exception is `src/tools/cookbook.py`, which imports
`validate_remote_host` and `validate_ssh_port` from `routes/_validators.py` at
module scope.

A second, structurally different case is `src/interactive_gate.py`, whose
`_has_active_chat_stream()` reaches into `routes.chat_routes._active_streams`
to decide whether foreground model work is in progress. That is an inverted
dependency on a route module's *mutable module state*, not just a function.

### Violation 2: `core/` reaches into `src/`

Fifteen lines, and unlike the above, several are at module top level:

- `core/constants.py` is now a pure re-export shim: `from src.constants import *`.
  The path constants live in `src/`, so anything in `core/` that needs a path
  imports upward. `core/database.py` and `core/auth.py` both do.
- `core/database.py` imports `src.runtime_paths.get_app_root` at top level and
  `src.secret_storage.encrypt/decrypt` inline from the `EncryptedText` column
  type.
- `core/session_manager.py` imports `src.attachment_refs` and
  `src.upload_handler` at top level.
- `core/__init__.py` imports from `src.llm_core`.

This one is arguably a naming problem rather than a design problem: `core/` is
not a lower layer than `src/`, it is a *subset* of `src/` that happens to hold
the ORM. `src/constants.py`'s own docstring calls itself the single source of
truth and `core/constants.py` the backward-compatibility shim, which
acknowledges the direction.

### Violation 3: `services/` is not a leaf

`services/__init__.py` describes services as plug-in capabilities that "can
run in-process or as a standalone HTTP service", which implies no dependency
on the app around them. In practice `services/` imports `src/` 36 times and
`core/` 6 times, and `services/memory/skill_extractor.py` imports
`routes.prefs_routes._load_for_user`.

The relationship also runs the other way in places: `src/search/*` are
compatibility shims that `sys.modules`-swap themselves for
`services.search.*` (see the docstring in `src/search/core.py`), so the same
code is reachable under two import paths.

### What is genuinely settled

- The router surface is uniform: every route module is wired in through its
  `setup_*_routes(...)` factory. `app.py` reaches past that interface exactly
  twice — `SESSION_COOKIE` from `routes/auth_routes.py` at top level, and
  `run_scheduled_skill_audit` from `routes/skills_routes.py` inside the
  nightly-audit loop.
- Nothing imports `mcp_servers/` from the app at runtime; they are executed as
  scripts. They do import `src.constants` themselves, which is why they resolve
  the same `DATA_DIR`.
- `static/js/` has no build step and therefore no import relationship with the
  Python side at all beyond the URLs in `static/index.html`.

---

## 4. Where state lives

### SQLite — `data/app.db`

The engine is created in `core/database.py` from `DATABASE_URL`, defaulting to
`sqlite:///<DATA_DIR>/app.db`. Two things about its configuration matter:

- The only PRAGMA set is `foreign_keys=ON`, applied by a `@event.listens_for(Engine, "connect")`
  hook. **WAL is not enabled**; the database runs in SQLite's default rollback-journal
  mode. The comment about `-wal`/`-shm` sidecars in `_SQLITE_SIDECARS` is
  conditional ("once WAL is enabled"), not a statement that it is.
- Secret-bearing columns use `EncryptedText`, a `TypeDecorator` that
  Fernet-encrypts on write and decrypts on read via `src/secret_storage.py`.
  This protects a stolen file, not a live process.

The tables and their owners:

| Table(s) | Written by | Owner-scoped |
|---|---|---|
| `sessions`, `chat_messages` | `core/session_manager.py`, chat routes | `sessions.owner`; messages inherit via session |
| `documents`, `document_versions` | `routes/document/` | `documents.owner` |
| `memories` | **writer not established** — read by `src/builtin_actions.py`; the live memory store is `data/memory.json` (see below) | yes (column exists) |
| `scheduled_tasks`, `task_runs` | `src/task_scheduler.py`, `routes/task_routes.py` | `scheduled_tasks.owner`; `task_runs` inherits |
| `crew_members` | scheduler + agents routes | yes |
| `model_endpoints`, `provider_auth_sessions` | `routes/model_routes.py` | yes |
| `email_accounts` | `routes/email_routes.py` (read by the email MCP child) | yes |
| `gallery_albums`, `gallery_images` | gallery routes | yes |
| `calendars`, `calendar_events`, `caldav_deleted_events` | calendar routes, `src/caldav_sync.py` | calendars and deletions yes; `calendar_events` inherits from its calendar |
| `api_tokens` | `routes/api_token_routes.py` | yes |
| `usage_ledger_entries` | LLM call paths | yes |
| `comparisons`, `signatures`, `editor_drafts`, `user_tools`, `user_tool_data`, `integrations` | their respective routes | mostly yes |
| `mcp_servers`, `webhooks` | `routes/mcp_routes.py`, `routes/webhook_routes.py` | **no owner column — global** |
| `notes` | legacy | superseded by the vault; see below |

`owner` is `nullable=True` everywhere it exists, meaning "legacy / shared".
A startup sweep and an hourly loop (`_migrate_assign_legacy_owner`, scheduled
in `app.py`'s `_null_owner_sweep_loop`) reassign null-owner rows to the admin,
because data created while auth was disabled would otherwise stay
world-visible.

Two more SQLite files sit beside `app.db`: `data/scheduled_emails.db` and
`data/email_cache.db` (`src/constants.py`). These are opened with raw
`sqlite3`, not SQLAlchemy, and `scheduled_emails.db` is the one file with
two writing processes (see §1).

### JSON files under `data/`

Every path is a named constant in `src/constants.py`; `CONTRIBUTING.md`
requires using the constant rather than re-deriving the path.

| File | Written by | Per-user | Survives restart |
|---|---|---|---|
| `auth.json` | `core/auth.py` | holds all users | yes |
| `settings.json` | `src/settings.py` | **no — global** | yes |
| `features.json` | `src/settings.py` | no | yes |
| `user_prefs.json` | `routes/prefs_routes.py` | yes, under a `_users` map | yes |
| `presets.json` | `src/preset_manager.py` | **no — global** | yes |
| `integrations.json` | `src/integrations.py` | no | yes |
| `contacts.json` | `routes/contacts/contacts_routes.py` | **no — global** (a flat `{"contacts": [...]}` list) | yes |
| `skills.json` + `data/skills/` | `services/memory/skills.py` | owner recorded in each `SKILL.md` | yes |
| `bg_jobs.json` + `data/bg_jobs/` | `src/bg_jobs.py` | no | yes — deliberately, status comes from on-disk exit codes |
| `cookbook_state.json` | `src/builtin_actions.py`, `src/cookbook_serve_lifecycle.py` | no | yes |
| `claude_code_tasks.json` | `src/agent_tools/claude_code_tools.py` | no | yes |
| `vault.json` | `routes/vault/vault_routes.py`, `src/tools/vault.py` | **no — global** (Vaultwarden CLI config) | yes |
| `embedding_endpoint.json` | `routes/embedding_routes.py`, read by `src/embeddings.py` | no | yes |
| `tidy_calendar_state.json` | `src/builtin_actions.py` | no | yes |
| `data/agent_activity/*.jsonl`, `runs.json` | `src/agent_activity.py` | per session | yes, rotated at 2 MB |
| `memory.json` | `MemoryManager` (`src/memory.py`, re-exported by `services/memory/`) — the live memory store, written by the memory routes and the memory MCP server | yes, `owner` per entry | yes |

Writes mostly go through `core/atomic_io.py` (`atomic_write_json` /
`atomic_write_text`: temp file, `fsync`, `os.replace`), which exists because a
plain truncate-then-write turns a kill -9 into an empty `auth.json`.
`routes/prefs_routes.py` open-codes the same pattern rather than importing it.

`sessions.json` is a special case: the constant still exists and is still
passed to `SessionManager.__init__`, but sessions live in SQLite and
`SessionManager.save_sessions()` is documented as "No-op for DB
compatibility". Call sites throughout the chat routes still call it.

### The Markdown vault

Notes and vault documents are one Markdown store under
`data/personal_docs/` (`PERSONAL_DIR`), configured by the `vault_directory`
and `notes_directory` settings. `routes/note/note_routes.py` writes through
`src/notes_store.STORE`; the codec is `src/notes_markdown.py`. The SQLite
`notes` table is legacy — `src/notes_vault_migration.py` copies rows out to
`.md` files and deliberately never deletes them, so the migration is
reversible.

Per-directory sensitivity labels (`public` / `private`) come from the
`vault_folder_sensitivity` setting and `ODYSSEUS_PERSONAL_DIRS`, and are what
actually closes a directory to the file tools. See
[`docs/vault-retrieval.md`](vault-retrieval.md) for indexing, ranking and the
path rules, and [`specs/personal-directories-and-tool-routing.md`](../specs/personal-directories-and-tool-routing.md)
for the governing behaviour spec.

### Chroma collections

Chroma holds vectors only; the source of truth is always elsewhere (the vault
files, `data/memory.json`, the tool schema definitions, the tool-output files
on disk). Base names:

| Collection base | Source of truth | Module |
|---|---|---|
| `odysseus_rag` | vault / personal-document files | `src/rag_vector.py` |
| `odysseus_memories` | `data/memory.json` | `src/memory_vector.py` |
| `odysseus_tool_index` | tool schemas | `src/tool_index.py` |

Each is suffixed with a lane name (`<base>_<lane>`, `src/embedding_lanes.py`).
Per [`specs/retrieval-runtime.md`](../specs/retrieval-runtime.md), the only
supported lane is `fastembed`: `build_embedding_lanes()` returns exactly one
lane and `migrate_legacy_collection()` is a documented no-op. Collection
metadata carries a fingerprint of model + url + dimension, and a lane whose
fingerprint no longer matches is reset and rebuilt.

Losing Chroma loses no user data. It loses retrieval until it is rebuilt.

### In-memory state that does not survive a restart

| State | Module | Consequence of loss |
|---|---|---|
| Hydrated sessions | `SessionManager.sessions` | reloaded from DB on demand |
| Active runs + replay buffers | `agent_runs._RUNS` | an in-flight agent turn is lost; `/api/chat/resume` 404s |
| In-flight stream bookkeeping | `chat_routes._active_streams` | partial-save safety net resets |
| Pending tool approvals and grants | `src/tool_approvals.py` `_PENDING`, `_GRANTS` | a pending approval card can no longer be honoured |
| Steer queue | `src/agent_control.py` `_STEER` | queued steers vanish |
| API-token cache | `app.py` `_token_cache` | rebuilt from DB on next bearer request |
| Route latency samples | `src/route_latency.py` | diagnostics only |
| Settings / features TTL cache | `src/settings.py` (2 s) | none |
| Rate-limiter windows | `src/rate_limiter.py` | login attempt counters reset |
| Scheduler lane semaphores, `_executing` | `src/task_scheduler.py` | handled: `start()` marks orphaned `task_runs` as `aborted` and pushes overdue `next_run` forward 60 s |

`src/agent_activity.py` is the hybrid: an in-memory deque per session backed
by a rotated JSONL file, so the feed survives but the live SSE subscribers do
not.

### Browser localStorage

Roughly 260 `localStorage` references across `static/js/`. It holds two
different kinds of thing, and the distinction is deliberate:

- **First-paint cache for account preferences.** `static/js/theme.js` reads
  `odysseus-theme` inline from `index.html` before any module loads, but the
  account copy lives server-side at `/api/prefs`. `static/js/serverPrefs.js`
  reconciles them newest-wins using an `{ value, updated_at }` envelope, and
  refuses to push a local copy up when `odysseus-auth-user` says the stored
  copy may belong to a previous account.
- **Genuinely local UI state.** Remembered modal dock side
  (`odysseus-modal-remembered-dock-<id>` in `modalManager.js`), collapsed
  sections, per-panel toggles. These are per-browser and are not synced.

### Durability and multi-user hazards

- **SQLite without WAL.** Under the default journal mode a writer blocks
  readers on the same file. With a single process and the Python
  `sqlite3` driver's default 5-second connect timeout this is usually
  invisible, but a long write
  transaction during an agent turn can stall unrelated reads.
- **`settings.json` is global.** `src/settings.py` has no per-user dimension,
  so operator-level and user-level choices share one file. On a multi-user
  install, one user changing a model or a feature gate changes it for
  everyone.
- **`user_prefs.json` is read-modify-write with no lock.**
  `routes/prefs_routes.py` `_save_for_user` loads the whole file, mutates one
  user's slot and writes it back. Two concurrent writes for different users
  can lose one of them. The write itself is atomic; the read-modify-write is
  not.
- **`mcp_servers` and `webhooks` rows have no owner.** Any authenticated user
  who can reach those routes sees and can change every user's entries.
- **Generated images are keyed by content hash only.** The route comment in
  `app.py`'s `serve_generated_image` notes that anyone who guesses a 12-hex
  hash could pull another user's image bytes.

---

## 5. Frontend

### No build step

`static/index.html` loads raw ES modules with `<script type="module">` tags —
33 of them, in a hand-maintained order with two ordering constraints
written into the file as comments: `models.js` must come before `app.js`, and
`app.js` must be last. Cache-busting is a manual `?v=<date><label>` query
string on the modules that changed.

Because there is no bundler, the server has to do the cache work instead.
`_RevalidatingStatic` in `app.py` subclasses Starlette's `StaticFiles` and
forces `Cache-Control: no-cache` on `.js`, `.css` and `.html` — the bytes stay
cached but every load revalidates, so a deploy does not require a hard
refresh. ETags still make the common case a 304.

Third-party code comes from two places: `static/lib/` (highlight.js, vendored)
and `cdn.jsdelivr.net` (KaTeX, Mermaid), which is why the CSP in
`core/middleware.py` allows `https://cdn.jsdelivr.net` for `script-src`,
`style-src` and `font-src`.

### The CSP nonce

Inline `<script>` blocks in `index.html` carry `nonce="{{CSP_NONCE}}"`.
`SecurityHeadersMiddleware` mints a fresh nonce per request into
`request.state.csp_nonce`; `src/app_helpers.serve_html_with_nonce()` reads the
HTML off disk and substitutes it before returning an `HTMLResponse`. The pages
served this way are fixed, server-owned templates, which is why a read failure
is mapped to a 500 rather than a 404.

`style-src 'unsafe-inline'` is retained deliberately — `index.html` ships
inline `<style>` blocks and several modules build runtime `style=""`
attributes.

### The service worker

`static/sw.js` is registered from an inline script at the very bottom of
`index.html`. Three strategies, by resource class:

| Resource | Strategy |
|---|---|
| Navigation to `/` only | stale-while-revalidate |
| `/static/**.js`, `**.css` | network-first, cache fallback |
| Other `/static/**` (images, fonts, libs) | cache-first with background refresh |
| `/api/**` and any non-GET | never cached |

JS and CSS are network-first specifically so a code change appears on a normal
reload. The navigation rule is scoped to `/` and not to `request.mode ===
'navigate'` in general, because the broader version served the SPA shell in
place of deep-linked `/static/*.html` pages.

`CACHE_NAME` is a manual version string (currently
`odysseus-v387-account-prefs`) and must be bumped whenever `PRECACHE` or the
worker's logic changes; `activate` deletes every cache that is not the current
name. `install` uses individual `cache.put` calls rather than `addAll` so one
404 cannot abort the whole precache.

The `PRECACHE` list is a second hand-maintained copy of the module list from
`index.html`, and the comment says so.

### How modules talk to each other

Three mechanisms, in descending order of how much of the app uses them:

1. **Direct ES imports** — 374 top-level `import` lines across
   `static/js/*.js` and `static/app.js`.
   This is the normal case: `theme.js` imports `storage.js`, `serverPrefs.js`,
   `colorPicker.js`, `windowDrag.js`, `tileManager.js`.
2. **Globals on `window`** — 117 assignments. `app.js` exposes a handful of
   modules deliberately — `window.themeModule`, `window.sessionModule`,
   `window.uiModule`, `window.adminModule`, `window.cookbookModule` — for what
   `static/js/MODULE_SUMMARY.md` calls "legacy inter-module reachability".
   The rest are cross-module flags with
   names like `window.__odysseusChatBusy`, `window.__odysseusSetPlanMode`,
   `window._emailSendDelegatedBoundV3` — mostly "have I already bound this
   listener" guards and shared mutable state between modules that do not
   import each other.
3. **Custom events** — about 40 `new CustomEvent` sites, concentrated in
   `chat.js`, `settings.js`, `cookbookRunning.js`, `admin.js` and
   `workbench.js`. Used where a module needs to announce something without
   knowing who cares.

`static/js/MODULE_SUMMARY.md` is the module-by-module map and is kept
alongside the code; it is the right starting point for "which file does X".

### Windows and modals

`static/js/modalManager.js` is the window manager. Tool panels register
themselves with `Modals.register(id, { restoreFn, closeFn, railBtnId })`, and
the manager owns the distinction the UI depends on: the `_` button and a
swipe-down **minimize** (hidden, JS state preserved), while `✕` **closes**
(runs `closeFn`, full teardown). A rail-button click cycles
closed → open → minimized → restored.

It delegates the spatial behaviour to siblings: `tileManager.js` (snap zones),
`modalSnap.js` (edge docking), `toolWindowZOrder.js` (a monotonic counter
starting at 300, because static CSS z-indexes would otherwise leave a restored
window behind an already-open one), `windowDrag.js` and `windowResize.js`.
Escape is arbitrated in `ui.js` for modals; overlays built ad hoc outside the
`.modal` system (dropdowns, context popups appended to `<body>`) register a
dismiss callback with `escMenuStack.js` instead, because the global arbiter
cannot find them.

### Theme

`static/js/theme.js` holds the preset table (`THEMES`: 16 presets, each a
`{ bg, fg, panel, border, red }` tuple with optional `advanced` overrides) and
the custom-theme editor. It is an account-scoped preference synced through
`serverPrefs.js`, cached in `localStorage` under `odysseus-theme` for first
paint. The actual styling is CSS custom properties in `static/style.css`.

### The size problem

| File | Bytes | Lines |
|---|---|---|
| `static/style.css` | 1.39 MB | 42,567 |
| `static/js/document.js` | 504 KB | 11,200 |
| `static/js/emailLibrary.js` | 400 KB | 8,510 |
| `static/js/settings.js` | 364 KB | 6,658 |
| `static/js/slashCommands.js` | 284 KB | 6,596 |
| `static/js/chat.js` | 294 KB | 6,108 |
| `static/js/notes.js` | 261 KB | 5,661 |
| `static/app.js` | 199 KB | 4,665 |

161 `.js` files under `static/js/` (86 of them at the top level), about
138,000 lines, plus `static/app.js`. Counts drift; recompute with
`find static/js -name '*.js' -print0 | xargs -0 wc -l`. The CSS is the single
worst offender and is tracked separately from the JS.

The seams that already exist are worth knowing, because they show the intended
direction of travel. Several subsystems have been partly extracted into
sibling directories while the monolith kept its name:
`static/js/emailLibrary.js` beside `static/js/emailLibrary/`,
`calendar.js` beside `calendar/`, and dedicated packages for
`markdown/`, `editor/`, `model/`, `research/`, `compare/`, `color/` and
`util/`. Chat is the cleanest split already done — `chat.js` (controller),
`chatStream.js` (SSE event handlers extracted from `handleChatSubmit` —
`ui_control` events and background-stream management), `chatRenderer.js`
(rendering),
`streamingRenderer.js` / `streamingSegmenter.js` (progressive output). The
window-management cluster (`modalManager`, `tileManager`, `modalSnap`,
`toolWindowZOrder`, `windowDrag`, `windowResize`, `escMenuStack`) is another
genuinely modular corner.

The files that resist splitting are the ones that are a UI surface *and* its
data layer *and* its network code at once — `document.js`, `emailLibrary.js`,
`settings.js`. There is no build step to hide the cost of splitting them, but
also nothing preventing it: a new `static/js/<feature>/` directory and one
more `<script type="module">` line is the whole ceremony.

---

## 6. Concurrency and background work

### One event loop

Everything runs on uvicorn's asyncio loop. There is no process pool, no
Celery, no separate worker. The two escape valves are:

- **`asyncio.to_thread`** — 84 call sites, plus 8 `run_in_executor`.
  Used wherever a synchronous library would otherwise block the loop: SQLite
  work on hot paths (`_refresh_token_cache` in `app.py`), Chroma's
  synchronous client (`src/agent_tools/rag_tools.py`), the CalDAV library
  (`src/caldav_sync.py`), filesystem walks, IMAP.
- **`asyncio.create_task`** — 53 sites. Fire-and-forget work: the token
  `last_used_at` touch, the foreground-gate's background-task sweep, post-
  response memory extraction, the scheduler's per-task dispatch.

Note that most DB access is *not* offloaded. SQLAlchemy sessions are opened
and closed synchronously inside handlers and inside scheduler coroutines.
On SQLite with a local file that is normally fast enough to be invisible; it
is also why `_SlowRequestLogMiddleware` exists.

`_RequestTimeoutMiddleware` is the backstop for the failure this design
invites: one handler that blocks the loop — a `subprocess.run` without a
timeout, an `httpx` call with no deadline — would otherwise freeze the server
for every user. After `REQUEST_HARD_TIMEOUT` (45 s default) the request is
abandoned with a 504.

### The scheduler's lanes

`src/task_scheduler.py` used to have one run slot. It now has three, each with
its own semaphore and its own operator-tunable bound:

| Lane | Default cap | Setting | What belongs in it |
|---|---|---|---|
| `model` | 1 | `task_model_lane_concurrency` | Anything that drives an LLM or loads a model |
| `external` | 2 | `task_external_lane_concurrency` | Bounded I/O against something outside the process |
| `maintenance` | 2 | `task_maintenance_lane_concurrency` | Local housekeeping — cleanup, indexing, health |

All caps are clamped to `LANE_CONCURRENCY_MAX = 8`, so a mistyped setting
cannot fork-bomb the host.

Classification is by explicit membership, in `_lane_for_task`. Three
frozensets decide it: `_MODEL_BACKED_ACTIONS` (the built-in actions that call
a model — `summarize_emails`, `draft_email_replies`, `classify_events`,
`audit_skills`, `consolidate_memory` and others), `_MAINTENANCE_ACTIONS`
(`tidy_sessions`, `tidy_documents`, `tidy_research`) and `_EXTERNAL_ACTIONS`
(`daily_brief`, `ssh_command`, `run_script`, `run_local`).
**Everything else lands in `model`** — an LLM task, a research task, a
task with a blank `task_type`, an unrecognised action, a task row that could
not be read. The docstring is explicit about why the fallthrough goes that
way: being wrong toward `model` costs a task some queue time, being wrong the
other way runs two LLM jobs on a box sized for one.

The model lane *is* the old single slot. `self._run_semaphore` and
`self._concurrency_cap` remain as aliases pointing at it, and
`_lane_semaphore()` adopts a pre-set `_run_semaphore` rather than creating a
second object beside it — which matters because tests construct the scheduler
with `__new__` and hand-set that attribute.

`lane_status()` derives `running` from each lane's semaphore rather than
counting separately, so the number cannot drift from the thing doing the
gating; `queued` is everything admitted to the lane that is not holding a
slot.

### The scheduler loop

`_loop()` sleeps 10 s, then polls. Each tick calls `_check_due_tasks()`, which
queries active tasks with `next_run <= now`, skips anything already in
`_executing`, and dispatches the rest as separate tasks. It then sleeps until
the next `next_run`, capped at 60 s — so a `* * * * *` cron task does not fire
up to a minute late.

`start()` also repairs what a crash left behind: `task_runs` rows still marked
`running` or `queued` are set to `aborted` (not `error` — the task is not to
blame for an infrastructure event), and active tasks whose `next_run` is
already in the past are pushed forward 60 s so the same overdue task does not
fire once per poll.

Beside the main loop it runs `_note_pings_loop` and `_lotus_pings_loop`
(internal scanners that are not user-facing tasks), and `src/event_bus.py`
can fire event-triggered tasks from outside the schedule.

### Other background work

| Loop | Started in | Cadence |
|---|---|---|
| Background-job monitor | `src/bg_monitor.py` | 5 s; re-invokes the agent when a `#!bg` job finishes |
| Vault scanner | `src/vault_scan.py` | 30 s (`ODYSSEUS_VAULT_SCAN_SECONDS`) |
| Null-owner sweep | `app.py` | hourly |
| Nightly skill audit | `app.py` | ~02:00 local, batch of 8 |
| Cookbook serve lifecycle | `src/cookbook_serve_lifecycle.py` | kills scheduler-launched serves past their window |
| Email pollers | `routes/email_pollers.py` | `ODYSSEUS_INPROCESS_POLLERS` |
| Upload rate-limit cleanup | `routes/upload_routes.py` (`periodic_rate_limit_cleanup`) | hourly |

`src/bg_jobs.py` is the only background mechanism that deliberately survives a
restart: a job's status is derived from an on-disk exit-code file rather than
a live PID, and a job stays `{done, followed_up: False}` until the agent has
actually been re-invoked, so the monitor retries on the next tick instead of
silently doing nothing.

### The foreground-activity gate

`src/interactive_gate.py` implements the rule that background work yields to
the user, and it does so in two directions at once.

**Pushing background work out of the way.** Every tracked request makes
`_InteractiveActivityMiddleware` fire
`task_scheduler.stop_background_tasks_for_foreground()`, which cancels every
running scheduler task except manual "Run now" runs. That is deliberately
blunt.

**Holding background work back.** `_check_due_tasks()` calls
`has_foreground_activity()` before dispatching, and pushes anything due out by
15 minutes if the UI is busy. A task that has been admitted to a lane calls
`_wait_for_idle_before_slot()`, which marks its run row "Queued — waiting for
Odysseus to be idle…" and then awaits `wait_for_interactive_quiet()` *outside*
its lane slot, so a waiting task does not hold the one model slot hostage.

"Active" means any of four things: an in-flight tracked request, a tracked
request within the quiet window (`BACKGROUND_TASK_QUIET_MS`, 1500 ms), a
browser heartbeat within `BACKGROUND_TASK_BROWSER_ACTIVE_SECONDS` (45 s), or
an active chat stream.

The last one is the subtle one. `_has_active_chat_stream()` reaches into
`routes.chat_routes._active_streams` and `agent_runs._RUNS`, because a chat
stream is detached from the HTTP request that started it — the request has
long since returned while the model is still generating. Without this check a
background LLM task would compete with the user's live chat on the same local
model.

**Passive endpoints.** `should_track_interactive_request()` excludes a fixed
list of observability endpoints from counting as user intent:
`/api/activity/heartbeat`, `/api/tasks/notifications`, `/api/agents/overview`,
`/api/agents/approvals`, `/api/agents/stream`, `/api/chat/runs`,
`/api/chat/stream_status*`, `/api/prefs*` and others. The reason is recorded
in the module: the agents dashboard polls every 5–20 s *and* refreshes on the
`run_started` SSE event, which produced a livelock — a scheduled task started,
the dashboard noticed, its poll cancelled the task about 1.4 s in, and the
task re-queued to die the same way, paying for a full prompt every time.
Clients can also mark a timer-driven request with the `x-odysseus-poll`
header, honoured only for GET and HEAD.

**Manual runs are exempt.** `mark_manual_foreground_run()` sets a
`ContextVar`, which asyncio copies into child tasks, so a "Run now" and
everything it spawns skips every quiet-window wait. Without it a manual run
could never start: the tab the user clicked in keeps sending heartbeats.

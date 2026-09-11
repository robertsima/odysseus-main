# Odysseus Claude Code Integration

This directory contains the Claude Code skill bundle for Odysseus.

## User Flow

1. Open Odysseus Settings > Integrations.
2. Add a Claude Agent.
3. Copy the full setup commands shown after the generated token.
4. Toggle the tools Claude is allowed to use.
5. Configure the terminal Claude Code session:

```bash
export ODYSSEUS_URL=http://your-odysseus-host:7000
export ODYSSEUS_API_TOKEN=ody_generated_token
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "$CLAUDE_DIR"
curl -fsSL -H "Authorization: Bearer $ODYSSEUS_API_TOKEN" "$ODYSSEUS_URL/api/claude/plugin.zip" -o /tmp/odysseus-claude-skill.zip
python3 -m zipfile -e /tmp/odysseus-claude-skill.zip "$CLAUDE_DIR/"
```

Claude Code auto-loads skills from its active config directory (`~/.claude` by
default, or `CLAUDE_CONFIG_DIR` for the persistent container installation), so
the `odysseus` skill is available in any session that has `ODYSSEUS_URL` and
`ODYSSEUS_API_TOKEN` in its environment.

## What's in the bundle

- `skills/odysseus/SKILL.md` — the skill definition Claude Code reads.
- `skills/odysseus/scripts/odysseus_api.py` — small helper that calls the scoped
  `/api/codex/*` endpoints (these are the canonical scope-gated agent API; the
  `codex` path is historic and shared by all agent integrations).

## Bidirectional collaboration

Claude Code can call Odysseus through the scope-gated `/api/codex/*` endpoints
using the bundled helper. Run `capabilities` before making calls and grant only
the scopes needed by the Claude Agent token.

Odysseus can call the locally installed Claude Code binary through the native
`delegate_to_claude_code` agent tool. Delegation is admin-only, limited to Git
repositories at or one level below the approved roots (default
`/app/data/development` and `/app/data/agent_worktrees`), and never grants
push, sudo, or arbitrary shell permission.

Claude Code is **not** a chat model in Odysseus. `chat_with_model("claude")`
and `list_models` will never find it; the agent is routed to
`delegate_to_claude_code` for anything that mentions Claude Code, and the
chat-model tools answer a `claude` lookup with that pointer.

The tool takes an `action`:

| action | what it does |
|---|---|
| `status` | preflight: binary path/version/flags, whether it is signed in (via `claude auth status`; no token is read), approved roots and every checkout under them, the default repository, callback configuration, live job count, and a repair hint per missing piece |
| `list_repositories` | the approved checkouts/worktrees with their branch |
| `run` (default) | delegate and wait (`timeout_seconds`, 30–1800, default 900) |
| `start` / `poll` / `cancel` / `list` | the same job in the background: the primary agent keeps working, can run several repositories in parallel (jobs on one checkout queue), and picks the result up later |

`repository` may be omitted: the configured default, `ODYSSEUS_AGENT_SOURCE_REPO`,
the active workspace, or the only approved checkout is used, in that order. A
wrong path (for example `/app`, the application root, which is not a checkout)
is rejected with the approved roots and known repositories in the message.

The reply carries Claude's `result` text, `permission_denials` (what Claude
wanted but was not allowed — pushes, arbitrary shell, files outside the
checkout), the CLI metrics (`num_turns`, `total_cost_usd`, `session_id`), and
what git reports afterwards (`branch`, `commit`, `changed_files`, `clean`).

### Settings

Everything is configurable in **Settings > Tools > Claude Code delegation**
(admin-only, persisted in `settings.json`, no restart) with the `CLAUDE_CODE_*`
environment variables as the fallback:

| setting | env | meaning |
|---|---|---|
| `claude_code_binary` | `CLAUDE_CODE_BINARY` | path of the unmodified `claude` binary |
| `claude_code_home` | `CLAUDE_CODE_HOME` | `HOME` for the child; its own sign-in lives under `$HOME/.claude` |
| `claude_code_repository_roots` | `CLAUDE_CODE_REPOSITORY_ROOTS` | absolute directories whose checkouts may be delegated to |
| `claude_code_default_repository` | `CLAUDE_CODE_DEFAULT_REPOSITORY` | used when a delegation names no repository |
| `claude_code_max_concurrent_tasks` | `CLAUDE_CODE_MAX_CONCURRENT_TASKS` | aggregate Claude subprocess limit (default 2) |
| `claude_code_model` | — | Claude model alias passed with `--model` (empty = Claude Code's default) |
| `claude_code_restricted` | — | run with `--restricted` (default on): ignore hooks/MCP servers declared inside the checkout, confine file tools to it, refuse bypassPermissions |
| `claude_code_odysseus_url` / `claude_code_odysseus_token_file` | `CLAUDE_CODE_ODYSSEUS_URL` / `CLAUDE_CODE_ODYSSEUS_TOKEN_FILE` | callback into this Odysseus (below) |

The same card shows the live preflight (`GET /api/claude-code/status`) and
recent delegations (`GET /api/claude-code/tasks`), with a cancel button for
running ones.

The runner adapts to the installed version: `--permission-prompts none` is
passed when the binary supports it (2.1.259+; older builds deny prompts in
headless mode anyway), `--restricted` when supported and enabled. `--bare`
is deliberately **not** used: it skips the operator's own sign-in.

### Terms of use (why this shape)

Anthropic's Claude Code legal page permits running the **unmodified** binary
inside your own agent infrastructure as long as each user authenticates with
their own credentials (subscription sign-in or API key) and usage is neither
resold nor intermediated, and it expressly allows an end user to sign in to a
hosted, unmodified Claude Code with their own subscription. It forbids
third-party software from collecting, storing, or routing requests through
Claude.ai credentials. Odysseus therefore:

- runs the binary as published and never modifies it or its auth methods;
- never reads, stores, or forwards Claude's OAuth/session token or API key —
  the operator signs in once as the container user (`HOME=$CLAUDE_CODE_HOME`);
- does **not** offer "Claude" as a chat model backed by that sign-in. To chat
  with Claude models directly, add an Anthropic **API key** as a model
  endpoint (billed per token under the Commercial Terms).

`skills/dev/claude-code-delegation/references/terms-and-boundaries.md` keeps
the working summary; the linked Anthropic pages are the authority.

### Testing the integration

1. In chat (as admin): "Check the Claude Code integration status." The agent
   calls `delegate_to_claude_code` with `action=status`; `ready: true` means
   binary + sign-in + at least one approved checkout. Or open Settings > Tools
   > Claude Code delegation and press **Check status**.
2. Then: "Have Claude Code add a docstring to `core/atomic_io.py` in
   `/app/data/development/odysseus-main` and commit it." The reply names the
   branch, commit, and changed files; the tool card in the timeline shows
   Claude's own report.
3. For a longer job: "Start a background Claude Code task in the
   claude-code-integration worktree to …", then "poll it".

To let an Odysseus-delegated Claude process call back into Odysseus during the
same job, set `CLAUDE_CODE_ODYSSEUS_URL` and
`CLAUDE_CODE_ODYSSEUS_TOKEN_FILE`. The token file must be a private regular
file (mode `0600`) containing a fresh, scoped Claude Agent token. The token is
passed only in the child environment; it is never placed in argv or task
persistence. Install the skill under the launcher's `CLAUDE_CONFIG_DIR` (the
setup command above handles both standard and custom config directories).

That token is minted by Odysseus, not by Claude: Settings > Integrations >
**+ Add Integration** > **Claude Agent** creates one, shows it once, and lets
you toggle its scopes (turn **Vault** on for the shared context store). It
starts with `ody_`. Nothing about a Claude sign-in is involved — this
credential only lets a Claude Code session read back into Odysseus. Write it
into the token file as the user Odysseus runs as:

```bash
umask 077
printf '%s' 'ody_...' > /app/data/secrets/claude-code-odysseus.token.txt
chown "$PUID:$PGID" /app/data/secrets/claude-code-odysseus.token.txt
```

A `401` from `/api/codex/capabilities` means the file's contents are not a
live token (a placeholder, or a revoked one); a `403` means the token is real
but is missing the scope for that endpoint.

The same delegation is also reachable over HTTP, for callers outside a chat
session (automation, CI, another admin tool):

- `GET /api/claude-code/status` — the preflight described above.
- `GET /api/claude-code/tasks` — bounded recent-task rows (no output blobs);
  API tokens see their own tasks, admin sessions see all.
- `POST /api/claude-code/tasks` — body `{repository?, prompt, allowed_tools?,
  timeout_seconds?, model?, label?}`, returns `{task_id, status: "queued"}`.
- `GET /api/claude-code/tasks/{task_id}` — current status plus, once
  finished, Claude's output and the repo's resulting `branch`, `commit`,
  `clean`/`status` (changed files).
- `POST /api/claude-code/tasks/{task_id}/cancel` — kills the task's Claude
  Code subprocess if still running; idempotent on an already-finished task.

Jobs are serialized per repository (a second task against the same checkout
queues behind the first). Aggregate Claude subprocess concurrency is capped by
`CLAUDE_CODE_MAX_CONCURRENT_TASKS` (default `2`). Bounded task results survive an Odysseus restart — a
task that was mid-flight when the process restarted is reported as
`interrupted` rather than silently disappearing. No credentials or process
environment are ever persisted, only owner/repository/status and the bounded
result above. The task prompt is intentionally not written to disk.

A cookie-session caller must be an admin. An API-token caller needs the
`claude_code:write` scope (`claude_code:read` is enough for the `GET`); the
`claude_code_tasks` token profile grants exactly that.

## Sharing Odysseus's context store with Claude Code

The `/api/codex/*` API is how a Claude Code session reads the same data the
Odysseus agent uses. Claude Code is the client, Odysseus is the data server,
and no Anthropic credential is ever handled by Odysseus.

Reachable with the `claude_agent` token profile: todos, memory, calendar,
email (read/draft), the editor document library, the Cookbook serve surface,
and — since the vault endpoints below — the user's Markdown notes.

| endpoint | scope | what it returns |
|---|---|---|
| `GET /api/codex/vault/search?q=...&k=5` | `vault:read` | semantic hits across `ODYSSEUS_PERSONAL_DIRS` (Vault Mind, AI Mind, Journal, ...) as `{path, title, sensitivity, similarity, excerpt}` |
| `GET /api/codex/vault/document?path=...&offset=0` | `vault:read` | one indexed vault file, paged with `total_chars` / `has_more` |

Private-labelled directories (typically `Journal:private`) are withheld unless
the token also carries `vault:read_private`. That split exists because an
agent session ships retrieved text to a hosted provider — the same reason the
chat path gates private notes on `is_local_endpoint`. Only indexed files are
readable, so the document endpoint cannot be walked into a general filesystem
reader.

A token minted before these scopes existed will not have them. Regenerate the
Claude Agent token, or enable the vault toggle on the existing one, in
Settings > Integrations > Claude Agent.

Two places the session can run:

- **Inside the container**, as part of a delegation. Set
  `claude_code_odysseus_url` (`http://127.0.0.1:7000`) and
  `claude_code_odysseus_token_file`; the runner passes both to the child in
  its environment and allowlists the helper script, so a delegated job can
  search the vault mid-task.
- **On your own machine**, in a terminal. Export `ODYSSEUS_URL` (the LAN or
  tailnet address of the Odysseus host) and `ODYSSEUS_API_TOKEN`, then install
  the plugin bundle with the command Settings shows. Claude Code loads the
  `odysseus` skill from its config directory and calls back over the network.

## Scope enforcement

The token is scope-gated. Every tool surface is checked server-side in Odysseus,
so even if Claude tries to call a forbidden endpoint, it gets `403` until the
user enables the matching toggle in Settings > Integrations > Claude Agent.
The `claude_agent` token profile bundles the scopes a Claude Code session
typically needs against `/api/codex/*` (todos, documents, memory, and the
public vault).

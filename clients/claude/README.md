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
| `update` (alias `upgrade`) | update the configured binary with its own updater (below); optional `version`: `latest`, `stable`, or an exact version |

`repository` may be omitted: the configured default, `ODYSSEUS_AGENT_SOURCE_REPO`,
the active workspace, or the only approved checkout is used, in that order. A
wrong path (for example `/app`, the application root, which is not a checkout)
is rejected with the approved roots and known repositories in the message.

The reply carries Claude's `result` text, `permission_denials` (what Claude
wanted but was not allowed — pushes, arbitrary shell, files outside the
checkout), the CLI metrics (`num_turns`, `total_cost_usd`, `session_id`), and
what git reports afterwards (`branch`, `commit`, `changed_files`, `clean`).

### Settings

Everything is configurable in **Settings > Agent Tools > Claude Code delegation**
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
| `claude_code_auto_update` | `CLAUDE_CODE_AUTO_UPDATE` | off by default. On: a run refused with "version X or newer is required" triggers one `update` and one retry, when no other delegation is running and the failed run left the checkout untouched |

The same card shows the live preflight (`GET /api/claude-code/status`) and
recent delegations (`GET /api/claude-code/tasks`), with a cancel button for
running ones.

The runner adapts to the installed version: `--permission-prompts none` is
passed when the binary supports it (2.1.259+; older builds deny prompts in
headless mode anyway), `--restricted` when supported and enabled. `--bare`
is deliberately **not** used: it skips the operator's own sign-in.

### Updating Claude Code

The binary is not part of the image. It lives in the persistent data
directory (default `/app/data/claude-code/bin/claude`), typically installed
once with `npm install -g --prefix /app/data/claude-code
@anthropic-ai/claude-code` as the container user, or with the native
installer. When a model needs a newer client, a run fails with "Claude Code
X does not support this model; version Y or newer is required". The tool
returns that as `error_kind: "claude_code_outdated"` with
`installed_version`, `required_version`, and the fix, and `status` keeps
reporting it (`update_required`) until the binary is new enough.

`delegate_to_claude_code {"action": "update"}` (admin only), or
`POST /api/claude-code/update` with body `{version?, timeout_seconds?}`, runs
the updater that matches how the configured binary was installed. It uses
the same allowlisted environment as a delegation (`HOME`, `CLAUDE_CONFIG_DIR`,
proxy and CA variables, no server secrets):

- **npm install** (the binary resolves into
  `<prefix>/lib/node_modules/@anthropic-ai/claude-code`): `npm install -g
  --prefix <prefix> @anthropic-ai/claude-code@<version|latest>`. The bare
  `claude update` would target npm's global prefix in the image instead.
- **native install**: `claude update`, or `claude install <version>` when a
  version is given. If the configured path is a symlink pinned to one file in
  the native `versions/` directory, it is repointed to the newest version.

The reply carries `version_before`, `version_after`, the command, and its
redacted output, and the cached `--version`/`--help` probe is dropped. The
update is refused (HTTP 409) while any delegation is running, since it would
replace the binary under it. Delegations that start during the update wait
for it to finish.

The bash tool does not accept `claude update`, `claude --version`,
`npm install -g @anthropic-ai/claude-code`, or the install script. It points
at `action=update` or `action=status` instead: an npm or script install from
the shell creates a second copy that delegation never runs, and that copy is
lost when the container is recreated.

### Signing in without SSH

The binary in the container needs its own Claude sign-in. You can do it from
the browser instead of `docker exec`-ing into the NAS:

1. Settings > Tools > **Claude Code delegation** > pick the account type
   (Claude subscription, or Anthropic Console for API billing) > **Sign in**.
2. Odysseus runs `claude auth login --claudeai` (or `--console`) in the
   container, as the app user, with the delegation environment (`HOME`,
   `CLAUDE_CONFIG_DIR`, proxy/CA variables; no server secrets). The card
   shows the authorization link the CLI prints. Open it on your own
   computer and sign in with your Claude account.
3. The browser cannot reach the CLI's localhost callback inside the
   container, so the authorization page shows a code instead (Claude Code's
   documented fallback for SSH sessions and containers). Paste it into the
   card and press **Submit code**.
4. Odysseus writes the code to the waiting CLI's stdin. The CLI exchanges it
   with its own PKCE verifier, stores the login in
   `$CLAUDE_CONFIG_DIR/.credentials.json` (mode 0600, written by the CLI),
   and exits. Odysseus then runs `claude auth status`; the card shows the
   signed-in account, plan, and auth method, and **Check status** turns ready.

**Sign out** runs `claude auth logout`. It is refused while a delegation or
an update is running. If `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or
`CLAUDE_CODE_OAUTH_TOKEN` is set in the container, it takes precedence over
the stored sign-in. The card names the variable but never shows its value.

Guarantees:

- Odysseus never sees a Claude token. The login stays in the CLI's own store,
  as it would after an SSH login. The pasted code is useless without the
  CLI's verifier. Odysseus writes it to the process and drops it: it is not
  logged, stored, returned in a response, kept in browser storage, or put in
  chat/task history. The log records only state transitions
  (`[claude-login] session=… awaiting_code -> verifying`), so the diagnostics
  bundle cannot contain it either.
- One sign-in runs at a time. It expires after 10 minutes, and its process is
  killed on expiry, cancel, a new **Sign in**, or app shutdown. Starting is
  limited to once per 10 seconds.
- The routes (`GET /api/claude-code/login/status`, `POST
  /api/claude-code/login/start|code|cancel`, `POST /api/claude-code/logout`)
  accept only an admin's browser session. API tokens are refused whatever
  their scopes, and so is the internal token agent tools use for loopback
  calls. The in-app agent cannot start a sign-in or submit a code:
  `delegate_to_claude_code action=status` only tells the admin to use this
  card.

Why `claude auth login` and not `claude setup-token`: `auth login` is a plain
subcommand that reads the code from stdin, so it works over ordinary pipes
with no terminal. `setup-token` and the interactive `/login` are full-screen
terminal UIs that need a TTY. `setup-token` also prints a year-long token
that Odysseus would then have to store and pass to every run, which the
terms below rule out. To keep the credential out of the container entirely,
use the cloud runner.

If the card cannot start the CLI (binary missing, too old to have `auth
login`), fall back to `docker exec -it -u <PUID> odysseus
/app/data/claude-code/bin/claude auth login` with the same `HOME`.

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
  the operator signs in once as the container user (`HOME=$CLAUDE_CODE_HOME`),
  from Settings (above: Odysseus only relays the one-time code to the CLI) or
  over SSH;
- does **not** offer "Claude" as a chat model backed by that sign-in. To chat
  with Claude models directly, add an Anthropic **API key** as a model
  endpoint (billed per token under the Commercial Terms).

`integrations/claude-code/skills/claude-code-delegation/references/terms-and-boundaries.md` keeps
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
- `POST /api/claude-code/update` — updates the binary (see "Updating Claude
  Code"). Requires `claude_code:write` *and* an admin; 409 while a
  delegation or another update is running.

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

`diagnostics:read` is opt-in and in no profile: it lets the token download the
diagnostics bundle (redacted logs plus the config of the chats, workers and
loadouts they mention; never chat messages) so Claude Code can debug Odysseus
without you pasting log lines. It is honoured only for a token owned by an
admin. Enable *Diagnostics* on the Claude Agent token, then:

```bash
curl -H "Authorization: Bearer $ODYSSEUS_API_TOKEN" "$ODYSSEUS_URL/api/diagnostics/bundle?minutes=60" -o bundle.zip
```

See `website/agent-worktree.md` (Diagnostics bundle) for the layout.

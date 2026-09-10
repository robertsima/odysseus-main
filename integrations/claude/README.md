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
repositories below `CLAUDE_CODE_REPOSITORY_ROOTS` (defaults to the development
and agent-worktree roots), and never grants push, sudo, or arbitrary shell
permission. Configure `CLAUDE_CODE_BINARY` and `CLAUDE_CODE_HOME` when the
installation uses non-default paths.

To let an Odysseus-delegated Claude process call back into Odysseus during the
same job, set `CLAUDE_CODE_ODYSSEUS_URL` and
`CLAUDE_CODE_ODYSSEUS_TOKEN_FILE`. The token file must be a private regular
file (mode `0600`) containing a fresh, scoped Claude Agent token. The token is
passed only in the child environment; it is never placed in argv or task
persistence. Install the skill under the launcher's `CLAUDE_CONFIG_DIR` (the
setup command above handles both standard and custom config directories).

The same delegation is also reachable over HTTP, for callers outside a chat
session (automation, CI, another admin tool):

- `POST /api/claude-code/tasks` — body `{repository, prompt, allowed_tools?,
  timeout_seconds?}`, returns `{task_id, status: "queued"}`.
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

## Scope enforcement

The token is scope-gated. Every tool surface is checked server-side in Odysseus,
so even if Claude tries to call a forbidden endpoint, it gets `403` until the
user enables the matching toggle in Settings > Integrations > Claude Agent.
The `claude_agent` token profile bundles the scopes a Claude Code session
typically needs against `/api/codex/*` (todos, documents, memory).

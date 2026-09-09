# Agent worktree and gated publishing

The agent gets one persistent Git worktree it can edit and test in. Nothing
leaves the machine until a human, at the host, approves that exact commit.

Publishing is **off by default**. With the feature disabled the agent can still
create a worktree, edit, run tests and commit — it simply cannot push.

## How the flow runs

1. **Agent** starts or reuses a worktree on a branch under `agent/odysseus/`.
2. **Agent** edits and tests there, then commits.
3. **Agent** calls `request_publish`. This freezes the change and records the
   repository, branch, head commit, changed-file list and which of those files
   are sensitive. It pushes nothing.
4. **You** run the CLI on the host, read the file list, and approve. The CLI
   prints a one-time code.
5. **You** paste the code to the agent, which calls `publish`. Odysseus
   re-derives the change from git, spends the code, pushes the branch, and opens
   a **draft** pull request.

The agent has no action that grants an approval. The code exists only in your
terminal and in the agent's message from you.

## If it says "unauthorized" or "cannot authenticate"

Run the doctor. It asks GitHub directly and names the specific mistake:

```bash
scripts/odysseus-agent-worktree doctor
```

In a container, run it inside the container so it sees the same environment:

```bash
docker exec -it odysseus python scripts/odysseus-agent-worktree doctor
```

The four failures that actually happen:

- **The agent used `bash` to push.** The credential only exists inside the
  `manage_agent_worktree` publish path. A `git push` typed into the shell tool
  has no token and no credential helper, so it fails with an authentication
  error that looks like a bad App. Publishing must go through the tool.
- **The Client ID was pasted into `ODYSSEUS_GITHUB_APP_ID`.** The App ID is a
  short number on the app's settings page. The Client ID starts with `Iv1.` or
  `Iv23`.
- **The App ID was pasted into `ODYSSEUS_GITHUB_APP_INSTALLATION_ID`.** These
  are different numbers. The Installation ID is the last segment of the app's
  Configure URL. `doctor` lists every installation the app has, with its id.
- **The private key is not inside the container.** A host path such as
  `/etc/odysseus/agent-app.pem` does not exist in the container filesystem.

## Setup

Add to `.env` (see `.env.example` for the full block):

```bash
ODYSSEUS_AGENT_PUBLISH_ENABLED=1
ODYSSEUS_AGENT_REPO=your-org/your-repo
ODYSSEUS_AGENT_BASE_BRANCH=dev
```

The push URL is derived from `ODYSSEUS_AGENT_REPO`. There is no setting that
points the agent at a different host.

### Credentials

Preferred: a GitHub App installed on that one repository, with **Contents:
write** and **Pull requests: write**. Odysseus mints a one-hour installation
token per operation and holds it in memory only.

```bash
ODYSSEUS_GITHUB_APP_ID=123456
ODYSSEUS_GITHUB_APP_INSTALLATION_ID=87654321
ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH=/etc/odysseus/agent-app.pem
```

### Where the key goes in a container

The container can only read paths that are bind-mounted into it. On ZimaOS and
the Docker Compose setups, that is the data volume, mounted at `/app/data`. Put
the key there and point the variable at the in-container path:

```bash
ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH=/app/data/github-app.pem
```

Copy the `.pem` into the host directory that backs `/app/data` (on ZimaOS, the
`AppData/odysseus/data` folder), then tighten it:

```bash
chmod 600 /path/to/AppData/odysseus/data/github-app.pem
```

The agent's own file tools refuse to open private key material, so parking the
key under the data mount does not expose it to the agent.

Fallback, for a setup where a GitHub App is not practical: a fine-grained
personal access token scoped to the one repository, in
`ODYSSEUS_AGENT_GITHUB_TOKEN`. It is long-lived, so rotate it. Odysseus logs a
warning every time it uses one.

Tokens are never written to `.git/config`, a credential helper, a log line, or a
command line. They reach git through `GIT_CONFIG_*` environment variables scoped
to the one remote.

## Operator commands

```bash
scripts/odysseus-agent-worktree status
scripts/odysseus-agent-worktree requests
scripts/odysseus-agent-worktree show REQUEST_ID
scripts/odysseus-agent-worktree approve REQUEST_ID
scripts/odysseus-agent-worktree approve REQUEST_ID --allow-sensitive
scripts/odysseus-agent-worktree revoke REQUEST_ID
scripts/odysseus-agent-worktree gc --keep-days 30
```

`show` prints every changed file, marking sensitive ones with `!`. Read it
before approving.

## What an approval is bound to

An approval is a random secret, stored only as a salted SHA-256 hash, and tied
to five facts:

| Bound fact | Effect if it changes |
|---|---|
| repository slug | approval refused |
| branch | approval refused |
| head commit SHA | approval refused |
| changed-file digest | approval refused |
| sensitive-path digest | approval refused |

It also expires (default 15 minutes, `ODYSSEUS_AGENT_APPROVAL_TTL_SECONDS`,
clamped to 60–3600 seconds) and is consumed on first use. If the agent commits
again after you approve, the SHA moves and the approval is dead — ask for a new
request rather than re-approving blind.

The record is marked used *before* the push runs, so a crash mid-push costs a
second approval instead of leaving a live grant behind.

What is pushed is the approved **commit object**, not the branch ref
(`<sha>:refs/heads/<branch>`). A ref can move between the check and the push; a
SHA cannot.

### Why the approval records cannot be forged

Approval state lives under the data directory, which the agent's file tools can
otherwise reach. Two things close that:

- The state directory is on the file tools' sensitive-path deny list, so
  `read_file`, `write_file`, `edit_file` and `apply_patch` all refuse it.
- Each grant carries an HMAC over its expiry, its sensitive acknowledgement, its
  code hash and all five bound facts, keyed by `.approval_key` inside that same
  directory. A hand-written or edited grant fails verification and is reported
  as `pending` — not as an approval.

Delete `.approval_key` to invalidate every outstanding grant at once.

## Sensitive changes

Some files decide what runs, as whom, and with which capabilities. Changing them
needs a second acknowledgement: `approve` refuses without `--allow-sensitive`.

| Category | Covers |
|---|---|
| `workflows` | `.github/`, `.gitlab-ci.yml`, `Jenkinsfile` |
| `docker` | `Dockerfile*`, `docker-compose*`, `docker/`, `.dockerignore` |
| `deployment` | `*.service`, `deploy/`, `k8s/`, `helm/`, `requirements*`, `setup.py`, `pyproject.toml`, `package*.json`, start/launch/build scripts |
| `auth` | `auth.py`, `middleware.py`, `auth_routes.py`, `api_key_manager.py`, `session_manager.py`, anything under an `auth/` path or naming OAuth |
| `secrets` | `.env*`, `*.pem`, `*.key`, `*.p12`, `secrets/`, `secret_storage.py`, `.netrc`, `.npmrc` |
| `mcp_permissions` | `mcp_servers/`, `.claude/`, `.mcp.json`, `mcp_manager.py`, `tool_security.py`, `tool_policy.py`, anything naming permissions |

## Reading the app's own logs

The agent can tail Odysseus's logs to debug itself, through `read_app_logs`.
It is read-only, addresses logs by name within known log directories only, and
redacts credential-shaped content (Authorization headers, bearer tokens, API
keys, JWTs, URLs carrying userinfo or query keys) before returning lines.

Log directories searched, in order: `<data>/logs/`, `<repo>/logs/`,
`/tmp/odysseus-tmux/`.

## Access control

Both tools are admin-only. They are in `NON_ADMIN_BLOCKED_TOOLS` and in the
route-level admin gate, so a non-admin chat user cannot reach them. In plan mode
the worktree tool is blocked and log reading stays available, since diagnosing is
exactly what plan mode is for.

## Limits worth knowing

- One approval covers one push. Amending or adding commits invalidates it.
- A change touching more than 500 files is refused; split it.
- If the remote branch moved between the request and the publish, the publish is
  refused and you need a fresh request.
- Odysseus never merges. The pull request is always created as a draft.
- Publishing only works through `manage_agent_worktree`. Git commands the agent
  runs in `bash` have no credential by design and will fail to authenticate.
- The agent may still ask you to approve something it should not. The file list
  in `show` is the thing to read, not the agent's summary of it.
- **This gate does not contain an agent that has `bash`.** Unconstrained shell
  can read any credential on the host and push on its own, so on a deployment
  where that matters, disable `bash` and `python` for the agent. With shell
  enabled, treat this flow as a workflow and an audit trail rather than a
  security boundary.
- The push runs from the operator's checkout, not from the worktree, and pins
  TLS verification and an empty proxy through environment config. That stops a
  `.git/config` written inside the worktree from redirecting the credentialed
  request.

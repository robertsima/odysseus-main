# Orchestration end-to-end test

A real run of the admin-chat -> worker -> worktree -> sandboxed bash -> hand-back
flow, on Linux, against the app's HTTP API, with a scripted model. It is a
developer tool, not part of the pytest suite.

What it does, in order:

1. Copies this checkout to `$E2E_ROOT/app` (default `~/e2e`, wiped each run) and
   starts it on port 7801 with a throwaway data dir (`$E2E_ROOT/data`),
   in-process pollers and tasks off, ChromaDB pointed at nothing.
2. Builds the fixture: a bare "Umni" remote with a tiny package and a pytest
   test, a clone at `$E2E_ROOT/development/umni` (the configured repository
   root), and a separate dummy repo as `ODYSSEUS_AGENT_SOURCE_REPO`, so the
   project is not the app's own source.
3. Serves the remote over HTTPS as `https://git.e2e.test/robertsima/Umni.git`
   (`githost.py`, with `GITHUB_HOST=git.e2e.test` and a CA bundle in
   `SSL_CERT_FILE`), because `manage_git fetch` only talks to GitHub-style
   HTTPS remotes. This needs passwordless `sudo` (port 443, one `/etc/hosts`
   line); without it, or with `E2E_REMOTE=local`, the origin is the bare path
   and the fetch step fails by design (`unsupported_remote`).
4. Starts `mock_model.py` (OpenAI-compatible, port 7811, native tool calls).
5. `driver.py` sets up the admin account, registers the mock endpoint, saves
   two loadouts ("Odysseus Admin" with `manage_agent_loadout`; "Lead Engineer"
   with the git/worktree/file tools and bash, private vault access off), opens
   a chat in agent mode under the admin loadout and sends one message through
   `/api/chat_stream`. The mock then plays:
   * admin: `manage_agent_loadout` start "Lead Engineer" with the Umni task
     and workspace;
   * worker: `manage_git` fetch -> `manage_agent_worktree` start (base
     origin/main) -> `edit_file` + `write_file` in the worktree -> `bash`
     `cd <worktree> && python -m pytest -q` -> `manage_agent_worktree` diff ->
     final report;
   * admin follow-up (after the product hands the worker's result back): a
     reply quoting the worker's report.
6. The driver waits for the worker run and the follow-up, then checks the run
   status, each worker step's tool result, the worktree (location, branch
   `agent/umni/e2e-double`, base origin/main, files present there and not in
   the clone), the sandbox evidence in the app log, and the parent's reply.
   It prints PASS/FAIL per check and `RESULT: PASS|FAIL at step ...`.

## Running it

From Linux (bash, git, bubblewrap, and a system `python` with pytest, because
the sandboxed shell sees only `/usr`; on Fedora
`sudo dnf install python3-pytest python-unversioned-command`):

    bash scripts/e2e_orchestration/run.sh

From Windows, through the WSL distro that has the CI venv:

    wsl -d podman-machine-default -- bash /mnt/d/<checkout>/scripts/e2e_orchestration/run.sh

Environment knobs:

| Variable | Default | Meaning |
| --- | --- | --- |
| `E2E_ROOT` | `~/e2e` | Everything the run creates. Wiped at the start (only if it holds the `.e2e-harness` marker). |
| `E2E_VENV` | `~/ci-repro/venv` | Python venv with the app's requirements. |
| `E2E_VARIANT` | `main` | `workaround` runs pytest in the workspace clone instead of the worktree. |
| `E2E_REMOTE` | `auto` | `https` (fake GitHub host), `local` (bare path), `auto` = https when `sudo -n` works. |
| `E2E_WORKER_MODEL` | empty | Pin the worker loadout's model instead of inheriting the chat's. |
| `E2E_APPROVAL_MODE` | unset | Set `agent_approval_mode` (e.g. `ask_risky`). |
| `E2E_TIMEOUT` | `300` | Seconds to wait for the worker and the follow-up. |

A run takes about 15 seconds.

## Where to look when it fails

Everything is under `$E2E_ROOT/logs`:

* `report.json` -- the checks, with details;
* `mock_requests.jsonl` -- one line per model request: which conversation
  (`admin`, `worker`, `admin_followup`, `aux`), which scripted step it chose,
  and the previous step's tool result as the model received it;
* `requests/` -- the full body of every model request;
* `app.log` -- the app's log (copied from `$E2E_ROOT/data/logs`);
* `admin_stream.sse` -- the raw `/api/chat_stream` response;
* `app.stdout`, `mock.log`, `githost.log`.

A reply starting `E2E-MOCK-ERROR` means the conversation left the script; the
mock log says at which step and why.

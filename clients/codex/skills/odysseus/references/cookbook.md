# Cookbook serve: debugging a failing model launch

The Cookbook routes reproduce what a person does in Agamemnon > Cookbook: read which serves run, read their output to find why one crashed, change the launch command, relaunch, stop a stuck one. All routes are under `/api/codex/cookbook/`. Helper: `~/plugins/odysseus/scripts/odysseus_api.py`.

## Routes

- `GET /api/codex/cookbook/tasks`: active serve, download and install tasks (sessionId, type, status, repo_id, remoteHost, payload._cmd). Requires `cookbook:read`.
- `GET /api/codex/cookbook/servers`: configured servers (name, host, port, env type and path, model dirs). Requires `cookbook:read`.
- `GET /api/codex/cookbook/cached?host=<NAME>`: models already cached on that server (HF cache, Ollama, extra modelDirs). Call it before `serve`. Requires `cookbook:read`.
- `GET /api/codex/cookbook/presets`: saved serve presets (model, host, port, cmd). A saved preset usually has a working cmd, so try `preset NAME` before composing your own. Requires `cookbook:read`.
- `GET /api/codex/cookbook/output/{session_id}?tail=400`: the last N lines of the task's persistent log (preferred) or tmux pane (fallback). The log survives a vllm crash, so it returns the Python traceback after the shell prompt overwrites the pane. Requires `cookbook:read`.
- `POST /api/codex/cookbook/serve`: launches a serve task. Body matches `ServeRequest`: `{ repo_id, cmd, remote_host?, ssh_port?, env_prefix?, gpus?, platform? }`. The leading binary of `cmd` must be `vllm`, `python3`, `sglang`, `llama-server`, `ollama`, `node` or `npx`. Pass the bare binary and its arguments: the validator rejects `cd`, `source`, `&&`, `||`, `;` and `$(...)`, and the host's saved settings add the venv activation (`env_prefix`). Requires `cookbook:launch`.
- `POST /api/codex/cookbook/preset/{name}`: launches a saved preset with its saved cmd and host. Requires `cookbook:launch`.
- `POST /api/codex/cookbook/adopt`: registers an externally launched tmux session so the UI tracks it. Body `{ tmux_session, model, host?, port? }`. Use it when `serve` rejected a cmd and you launched over ssh and tmux instead. Requires `cookbook:launch`.
- `POST /api/codex/cookbook/stop/{session_id}`: kills the tmux session for that task. Requires `cookbook:launch`.

## Example

```bash
# Survey what is running
python3 ~/plugins/odysseus/scripts/odysseus_api.py cookbook tasks

# Read the failing one (sessionId from `cookbook tasks`)
python3 ~/plugins/odysseus/scripts/odysseus_api.py cookbook output serve-abc12345 400

# Stop the previous attempt before trying new flags
python3 ~/plugins/odysseus/scripts/odysseus_api.py cookbook stop serve-abc12345

# Relaunch with new flags: repo_id, cmd, then the host
python3 ~/plugins/odysseus/scripts/odysseus_api.py cookbook serve \
  <model-path-or-repo-id> \
  "vllm serve <model-path-or-repo-id> --host 0.0.0.0 --port 8001 --tensor-parallel-size <gpus> --max-model-len <tokens> --gpu-memory-utilization 0.90" \
  <user@host>
```

Take host, model path and flags from `cookbook servers`, `cookbook cached` and the saved presets.

## Debug loop

1. `cookbook tasks`: find the failing sessionId.
2. `cookbook output SID 600`: read the root-cause line. It often sits above the visible tail; request a larger `tail` when the error refers to "above".
3. `cookbook stop SID`: two serves on one `--port` collide, so stop the previous attempt first.
4. `cookbook serve repo "new cmd"`: try the next variation, wait about 20 seconds, then run `cookbook output` on the new sessionId.

## Limits

- `cookbook serve` accepts only the allowlisted model-server binaries, so it cannot run arbitrary shell.
- `cookbook stop` accepts only sessionIds matching `[a-zA-Z0-9_-]+`.
- A serve is a long-lived process that pins GPUs. Stop your previous attempt before relaunching and check `cookbook tasks` for another serve on the same `--port`.

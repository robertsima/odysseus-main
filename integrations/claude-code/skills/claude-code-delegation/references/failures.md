# Claude Code delegation failures

| Symptom | Meaning | Do |
|---|---|---|
| "not an existing Git repository or worktree" | wrong path (for example `/app`) | use a path from `status` or `list_repositories` |
| "outside Claude Code approved roots" | path not under the configured roots | pick an approved one, or ask the operator to add the root in Settings |
| `ready: false`, `auth.logged_in: false` ("Not logged in") | binary not signed in | if `cloud.ready` is true, delegate with `repository: "owner/repo"` (cloud runner; with `cloud.any_repository` any repository, and `via: "cloud"` alone uses the workspace's GitHub origin). Otherwise ask the admin to open Settings > Claude Code > **Sign in** and finish it in their browser (no SSH). You cannot do this step or handle the code for them, and the code never goes into chat |
| "binary unavailable" | wrong `claude_code_binary` | the operator fixes the path in Settings > Agent Tools > Claude Code |
| `error_kind: claude_code_outdated` ("Claude Code X does not support this model; version Y or newer is required") | installed CLI too old for the model | call `{"action": "update"}` (add `"version": "Y"` if the default channel stays too old), check `version_after`, retry the task once. Install and update only through that action: a bash install makes a second copy the delegation never runs |
| update refused: "run(s) are in progress" | an update would replace the binary under a running job | poll or cancel those task ids, then update |
| `permission_denials` mentions push or remote | Claude tried to publish | expected; publishing goes through `manage_agent_worktree` |
| exit 124 | timed out | narrow the task or split it |

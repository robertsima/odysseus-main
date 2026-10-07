# Todoist in Agamemnon

Call the built-in MCP function `mcp__todoist__todoist` (not a separate `td` MCP server). The wrapper runs `td <args...>` without a shell.

```json
{"args":["task","list","--json"]}
```

Commands:

```json
{"args":["today","--json"]}
{"args":["upcoming","--json"]}
{"args":["inbox","--json"]}
{"args":["task","list","--json"]}
{"args":["completed","--json"]}
{"args":["task","view","<task-id>","--json"]}
{"args":["task","update","<task-id>","--due","tomorrow","--json"]}
{"args":["task","complete","<task-id>","--json"]}
```

CLI and API facts:
- The official CLI is `@doist/todoist-cli`.
- `TODOIST_API_TOKEN` takes priority over stored CLI credentials.
- Pass `--json` or `--ndjson` for parseable output wherever available.
- Priority values are 1-4. In CLI shorthand `p1` is most urgent and maps to API priority 4.
- Full-day due dates use `YYYY-MM-DD`, floating due datetimes `YYYY-MM-DDTHH:MM:SS` (local), fixed due datetimes UTC with `Z`. Deadlines are date-only.

Safety:
- Never print or expose `TODOIST_API_TOKEN`.
- Ask before deleting, completing, bulk moving or rescheduling existing tasks, unless the user explicitly asked.
- During a retrospective, propose edits before applying them.

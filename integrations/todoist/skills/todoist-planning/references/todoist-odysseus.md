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
{"args":["task","add","Task title","--due","tomorrow","--priority","p2","--json"]}
{"args":["task","quickadd","Task title tomorrow p2","--json"]}
{"args":["task","update","<task-id>","--due","next Monday","--json"]}
{"args":["task","complete","<task-id>","--json"]}
```

CLI and API facts:
- The official CLI is `@doist/todoist-cli`.
- `TODOIST_API_TOKEN` takes priority over stored CLI credentials.
- Pass `--json` or `--ndjson` for parseable output wherever available.
- Priority values are 1-4. In CLI shorthand `p1` is most urgent and maps to API priority 4.
- Full-day due dates use `YYYY-MM-DD`, floating due datetimes `YYYY-MM-DDTHH:MM:SS` (local), fixed due datetimes UTC with `Z`. Deadlines are date-only.

Agamemnon calendar:
- Todoist tasks with due dates or deadlines appear in Agamemnon Calendar after Todoist calendar sync. Tasks without due dates do not appear.
- Todoist is the source of truth. Sync is pull-only, so editing a Todoist-derived calendar event does not write back.

Safety:
- Never print or expose `TODOIST_API_TOKEN`.
- Ask before deleting, completing, bulk moving or rescheduling existing tasks, unless the user explicitly asked.
- When the task list is emotionally loaded or ambiguous, propose edits before applying them.

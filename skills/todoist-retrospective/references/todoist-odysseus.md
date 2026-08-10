# Todoist in Odysseus

Use the built-in MCP function `mcp__todoist__todoist`.

Input shape:

```json
{"args":["task","list","--json"]}
```

The wrapper runs `td <args...>` without a shell. Do not call `td` as a separate MCP server.

Useful commands:

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

Todoist CLI/API facts:
- The official CLI is `@doist/todoist-cli`.
- `TODOIST_API_TOKEN` takes priority over stored CLI credentials.
- Use `--json` or `--ndjson` for parseable output whenever available.
- Todoist priority values are 1-4; in CLI shorthand, `p1` is most urgent and maps to API priority 4.
- Full-day due dates use `YYYY-MM-DD`.
- Floating due datetimes use a local datetime like `YYYY-MM-DDTHH:MM:SS`.
- Fixed due datetimes use UTC with `Z`.
- Deadlines are date-only.

Safety:
- Never print or expose `TODOIST_API_TOKEN`.
- Ask before deleting, completing, bulk moving, or rescheduling existing tasks unless the user explicitly asked for it.
- Prefer proposed edits over surprise changes during retrospectives.

Review frameworks:
- Eisenhower Matrix: diagnose whether the task list is being driven by urgency, importance, both, or neither.
- GTD: use review as the "reflect" step that keeps capture and organization trustworthy enough for clear engagement.
- Deep Life: ask whether completed and planned work served what matters in work, home, relationships, health, craft, or inner life.
- Kaizen: end retrospectives with one small process improvement, not a giant system overhaul.
- Justin Sung-inspired leverage: review productivity by outcome, not activity. Look for the few actions that created most progress, the few bottlenecks that created most friction, missing constraints that let work expand, and whether the user protected one key task.

Cadence:
- Weekly retrospective should identify meaningful progress, stale obligations, urgency traps, and one improvement for next week.
- Nightly retrospective should forgive or reschedule leftovers, make tomorrow's first action obvious, and reduce morning ambiguity.

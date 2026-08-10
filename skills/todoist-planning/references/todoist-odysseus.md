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
{"args":["task","add","Task title","--due","tomorrow","--priority","p2","--json"]}
{"args":["task","quickadd","Task title tomorrow p2","--json"]}
{"args":["task","update","<task-id>","--due","next Monday","--json"]}
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

Odysseus calendar behavior:
- Todoist tasks with due dates or deadlines can appear in Odysseus Calendar after Todoist calendar sync.
- Todoist remains the source of truth.
- Calendar sync is pull-only; editing a Todoist-derived calendar event does not write back to Todoist.
- Tasks without due dates do not appear on the calendar.

Safety:
- Never print or expose `TODOIST_API_TOKEN`.
- Ask before deleting, completing, bulk moving, or rescheduling existing tasks unless the user explicitly asked for it.
- Prefer proposing edits before applying them when the task list is emotionally loaded or ambiguous.

Planning frameworks:
- Eisenhower Matrix: sort tasks by urgency and importance. Do urgent + important work, schedule important + not urgent work, delegate/batch urgent + not important work, and delete/defer not urgent + not important work.
- GTD: capture what has attention, clarify what each item means, organize it into the right place, reflect regularly, then engage with the right next action.
- Deep Life: prefer intentional focus on what matters in work, home, and inner life over attention leakage and shallow busyness.
- Kaizen: make small, continuous improvements rather than redesigning the whole system at once.
- Justin Sung-inspired leverage: prioritize task management and high-yield work over generic time management. Use Pareto distribution to find the few actions that drive most outcomes, one-key-task focus to avoid scattered attention, and Parkinson's Law constraints so ambiguous work does not expand endlessly.

Cadence:
- Weekly planning should select a few meaningful outcomes, classify the task pool with Eisenhower, and schedule important + not urgent work before the week fills with urgency.
- Nightly planning should choose tomorrow's anchors, reduce ambiguity, and produce a first visible next action.

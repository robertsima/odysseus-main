---
name: todoist-retrospective
description: Review Todoist activity and recover momentum using the Eisenhower Matrix, weekly review, nightly next-day review, GTD reflect/engage, Cal Newport-style value-driven focus, Kaizen continuous small improvements, and Justin Sung-inspired high-leverage productivity principles such as Pareto distribution, task management over time management, Parkinson's Law constraints, one-key-task focus, and outcome-over-activity review. Use when the user asks for a daily/weekly retrospective, task triage, overdue cleanup, executive dysfunction support, burnout-aware planning, stuckness recovery, deciding what to do next, or reflecting on completed/unfinished Todoist work through the built-in Todoist MCP tool.
---

# Todoist Retrospective

Use this skill to turn Todoist state into a compassionate review and a next step. The goal is learning, values alignment, and momentum, not blame.

For exact Todoist/Odysseus command details and framework notes, read [references/todoist-odysseus.md](references/todoist-odysseus.md) when you need to call Todoist or explain the review model.

Optional retrospective templates live in `assets/templates/`. Use them when the user asks for a reusable weekly review, nightly review, or stuckness reset worksheet.

## Workflow

1. Pull the current picture.
   Use `mcp__todoist__todoist` with JSON output. Start with:
   - `{"args":["today","--json"]}`
   - `{"args":["upcoming","--json"]}`
   - `{"args":["inbox","--json"]}`
   - `{"args":["task","list","--json"]}`

2. Sort without judgment using Eisenhower.
   Group tasks as:
   - urgent + important: still needs direct attention
   - important + not urgent: protect or reschedule intentionally
   - urgent + not important: delegate, batch, automate, or time-box
   - not urgent + not important: drop, park, or ignore
   Also note overdue/stale, waiting/blocked, unclear/oversized, and probably-not-worth-doing items.

3. Identify friction.
   Look for vague verbs, missing next actions, too many priorities, due dates used as guilt, and tasks that hide multiple steps.

4. Review leverage, not just volume.
   Ask which few completed tasks created most of the value, which repeated activities produced little outcome, and which bottleneck kept everything else stuck. Treat busyness as data, not achievement.

5. Connect back to values.
   Ask what the user is trying to protect or become if the list feels arbitrary. Treat the calendar/task list as a bridge from values to action.

6. Recommend a reset.
   Prefer one of:
   - complete/delete/reschedule stale tasks
   - rewrite vague tasks into next actions
   - choose a minimum viable day
   - create a waiting-for follow-up
   - park nonessential work

7. Ask before destructive Todoist changes.
   Completing, deleting, renaming, rescheduling, or moving existing tasks changes the user's system. Propose the changes first unless the user clearly asked you to act.

## Review Cadence

Weekly retrospective:
- What mattered this week?
- Which important but not urgent work got protected?
- Which urgent but not important work hijacked attention?
- Which 20% of actions produced most of the meaningful results?
- Which recurring activities looked productive but did not move outcomes?
- What should be delegated, batched, deleted, or moved to someday?
- What is one Kaizen improvement for next week?

Nightly review:
- What is still open from today?
- What deserves forgiveness, rescheduling, or deletion?
- What was the one key task, and did it get real attention?
- What is the first task for tomorrow?
- What can be made smaller before sleep?

## Retrospective Prompts

Use a small set of questions:
- What actually moved?
- What kept showing up?
- What was blocked by missing information, energy, or a decision?
- What should be made smaller?
- What should be forgiven and rescheduled?
- What is the next kind action?
- What small system improvement would make tomorrow easier?
- What constraint would have prevented today's work from expanding?

## Executive Dysfunction Mode

If the user sounds stuck, scattered, ashamed, or overloaded:
- Start with validation and a concrete next move.
- Reduce choices to 2-3 options.
- Prefer "open the document" or "send one sentence" tasks.
- Suggest timers only gently.
- Convert "catch up on everything" into a short rescue plan.

Avoid:
- Long lectures
- Optimization-heavy systems
- "Just prioritize" without reducing ambiguity
- Creating many new tasks from anxiety

Use Kaizen gently: choose one small improvement to the workflow after each review. Examples: rewrite one recurring task, add one checklist, remove one stale due date, or protect one deep-work block.

## Justin Sung-Inspired Review Checks

Use these checks when reviewing a task list:
- Pareto: Which few actions created most of the progress?
- Anti-Pareto: Which few sources created most friction, procrastination, or rework?
- Parkinson's Law: Which tasks expanded because they had no constraint?
- One-key-task focus: Did the day/week have a clear primary task?
- Task management: Were tasks actionable, or did vague task wording create avoidance?
- Outcome over activity: What should be measured by result rather than time spent?

## Todoist Update Patterns

Use update/complete only after consent:

```json
{"args":["task","complete","<task-id>","--json"]}
```

```json
{"args":["task","update","<task-id>","--due","tomorrow","--json"]}
```

For unclear inbox items, propose rewrites first. If accepted, update the existing task instead of duplicating it.

## Output Shape

Return:
- What I see
- Eisenhower matrix readout
- Pareto/high-leverage readout
- What to keep
- What to change
- Suggested Todoist edits
- One Kaizen improvement
- One next action

Keep it short enough that the user can act immediately.

## Optional Templates

Use `assets/templates/weekly-retrospective.md` for weekly reviews.
Use `assets/templates/nightly-review.md` for end-of-day reviews.
Use `assets/templates/stuckness-reset.md` when the user is overloaded, avoidant, or trying to recover momentum.

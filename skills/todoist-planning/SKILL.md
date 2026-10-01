---
name: todoist-planning
description: Planning with Todoist: a day, week, next day or sprint plan, breaking vague goals into next actions, turning notes into tasks, prioritizing, scheduling follow-ups.
---

# Todoist planning

Turn messy intent into a small, realistic Todoist plan that connects values to daily actions. Call Todoist through `mcp__todoist__todoist`; command shapes, CLI facts and calendar behaviour are in [references/todoist-odysseus.md](references/todoist-odysseus.md).

## Workflow

1. **Capture and clarify.** Turn the user's open loops into candidate tasks, and vague items into outcomes and next actions. With enough context, assume reasonably and proceed.
2. **Inspect Todoist** when planning against current commitments: `{"args":["today","--json"]}`, `upcoming`, `inbox` or `task list`, as the plan needs.
3. **Sort with the Eisenhower Matrix before scheduling:**
   - urgent + important: do today or assign a concrete slot;
   - important + not urgent: schedule deep work and protect it;
   - urgent + not important: delegate, automate, batch or time-box;
   - not urgent + not important: delete, defer or park.
4. **Apply the leverage checks** (below) to find the few tasks that matter.
5. **Connect to values.** When priorities are unclear, ask which life or work value the plan serves. A few meaningful commitments beat a crowded list.
6. **Write each task as a next action**: a verb-led title, visible, concrete, easy to start. Add a due date only when it helps, a priority only when it changes behavior, and a description for context, acceptance criteria, links or a checklist.
7. **Fit capacity.** One day holds 1-3 anchor tasks, 2-5 small tasks and an explicit buffer. Label the excess later, parking lot or waiting.
8. **Create tasks only when the user asked or clearly consented.** Otherwise present the proposed list first.
9. After creating dated tasks, tell the user Odysseus Calendar shows them after Todoist calendar sync.

## Leverage checks

Run these before adding tasks (Justin Sung-style: manage tasks, not time):
- Pareto: which small subset of tasks produces most of the outcome?
- Bottleneck: which task makes the rest easier or unnecessary?
- One key task: what deserves undivided attention today?
- Task clarity: is the task itself clarified, or are we only moving time blocks around?
- Parkinson's Law: what constraint stops this task expanding? Give high-leverage and ambiguous tasks a clear time limit.
- Activity versus outcome: does this task create the result or only stand in for progress?

## Cadence

Weekly plan:
- Review last week and current Todoist commitments.
- Choose 2-4 outcomes that make the week meaningful.
- Eisenhower-sort the pool into do, schedule, delegate or batch, and drop.
- Name the week's leverage tasks (the few that unlock disproportionate outcomes or remove repeated friction).
- Date tasks only for real commitments, and leave someday/maybe work undated or parked.
- Reserve deep-work blocks for important + not urgent work.

Nightly plan:
- Pull `today`, `upcoming` and, if needed, the inbox.
- Choose tomorrow's 1-3 anchors and the one key task.
- Pick the first visible next action for each anchor.
- Move or soften stale due dates so tomorrow is believable.
- End with one "start here" task.

## Executive-function-friendly wording

Phrase tasks to lower the start cost: "Start by opening..." for "Finish...", "Draft rough outline" for "Write final proposal", "Send one message asking for..." for "Resolve...".

When the user sounds overwhelmed, stuck, avoidant or tired, slice smaller: a 2-minute starter, the next physical action, a "good enough" version, a recovery or admin task, a dated follow-up. Keep the tone neutral and aimed at motion.

Kaizen: change the system by one small adjustment at a time (one checklist, label, reminder or wording change) instead of rebuilding the user's setup.

## Creating tasks

Structured add when the fields are clear:

```json
{"args":["task","add","Draft rough outline","--due","tomorrow","--priority","p2","--json"]}
```

Quick add when natural language is clearer:

```json
{"args":["task","quickadd","Draft rough outline tomorrow p2 #Writing","--json"]}
```

For several tasks, issue one simple command each and report what was created.

## Output shape

Planning without writing:
- Eisenhower matrix summary
- leverage callout
- weekly or tomorrow anchors
- small next actions
- waiting, delegate and batch items
- parking lot and drop list
- one Kaizen improvement

Writing to Todoist:
- the created tasks with due dates and priorities
- anything intentionally left uncreated
- the next single action

## Templates

Offer them when the user wants a reusable artifact: `assets/templates/weekly-plan.md`, `assets/templates/nightly-plan.md`, and `assets/templates/capture-clarify.md` (emptying and clarifying a messy task pile before scheduling).

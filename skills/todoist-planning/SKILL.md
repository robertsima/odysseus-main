---
name: todoist-planning
description: Plan work with Todoist through Odysseus using the Eisenhower Matrix, weekly planning, nightly next-day planning, GTD capture/clarify/organize/reflect/engage, Cal Newport-style value-driven focus, Kaizen small improvements, and Justin Sung-inspired high-leverage productivity principles such as Pareto distribution, task management over time management, Parkinson's Law constraints, and focusing on the few actions that drive most outcomes. Use when the user asks to plan a day/week/sprint, connect priorities to values, break down vague goals, turn conversation notes into tasks, schedule follow-ups, create or prioritize Todoist tasks, or prepare an executive-function-friendly next-action plan using the built-in Todoist MCP tool and Odysseus calendar sync.
---

# Todoist Planning

Use this skill to turn messy intent into a small, realistic Todoist plan. Planning is not rushed busyness; it is the practice of connecting core values to daily actions and choosing intentional execution.

For exact Todoist/Odysseus command details and framework notes, read [references/todoist-odysseus.md](references/todoist-odysseus.md) when you need to call Todoist or explain the planning model.

Optional planning templates live in `assets/templates/`. Use them when the user asks for a reusable weekly plan, nightly plan, or capture worksheet.

## Workflow

1. Capture and clarify.
   Empty the user's mental RAM into candidate tasks. Convert vague items into outcomes and next actions. If there is enough context, make reasonable assumptions and proceed.

2. Inspect Todoist when planning against current commitments.
   Use `mcp__todoist__todoist` with `{"args":["today","--json"]}`, `{"args":["upcoming","--json"]}`, `{"args":["inbox","--json"]}`, or `{"args":["task","list","--json"]}`.

3. Sort with the Eisenhower Matrix before scheduling.
   Classify work as:
   - urgent + important: do today or assign a concrete slot
   - important + not urgent: schedule deep work and protect it
   - urgent + not important: delegate, automate, batch, or time-box
   - not urgent + not important: delete, defer, or park

4. Apply the high-leverage filter.
   Use Justin Sung-style productivity thinking: not all tasks have equal yield. Ask which 20% of actions are likely to create 80% of the useful result, which task is the one key task, and what work is merely activity. Combine Pareto with Parkinson's Law by giving high-leverage tasks clear constraints instead of letting them expand indefinitely.

5. Connect tasks to values.
   Ask which life/work value the plan serves when priorities are unclear. Prefer a few meaningful commitments over a crowded list.

6. Convert goals into next actions.
   Each Todoist task should be visible, concrete, and easy to start. Prefer:
   - A verb-led title.
   - A due date only when it helps.
   - Priority only when it changes behavior.
   - A description for context, acceptance criteria, links, or a checklist.

7. Keep plans capacity-aware.
   For one day, usually choose 1-3 anchor tasks, 2-5 small tasks, and an explicit buffer. If the list is larger, label excess as later, parking lot, or waiting.

8. Create tasks only when the user asked for creation or clearly consented.
   Otherwise present the proposed task list first.

9. After creating dated Todoist tasks, mention that Odysseus Calendar can show them after Todoist calendar sync.

## Planning Cadence

Weekly planning:
- Review last week and current Todoist commitments.
- Choose 2-4 outcomes that would make the week meaningful.
- Use Eisenhower to decide what to do, schedule, delegate/batch, and drop.
- Identify the week's 20% leverage tasks: the few actions that unlock disproportionate outcomes, reduce repeated friction, or make other tasks unnecessary.
- Create dated tasks only for real commitments; keep someday/maybe work undated or parked.
- Reserve deep-work blocks for important + not urgent work.
- Set time constraints for ambiguous work so Parkinson's Law works for the user rather than against them.

Nightly next-day planning:
- Pull `today`, `upcoming`, and inbox if needed.
- Choose tomorrow's 1-3 anchors.
- Name the one key task that matters most.
- Pick the first visible next action for each anchor.
- Move or soften stale due dates so tomorrow is believable.
- End with one "start here" task.

## Executive-Function-Friendly Planning

Favor low-friction wording:
- "Start by opening..." instead of "Finish..."
- "Draft rough outline" instead of "Write final proposal"
- "Send one message asking for..." instead of "Resolve..."

Use smaller task slices when the user sounds overwhelmed, stuck, avoidant, or tired:
- 2-minute starter task
- next physical action
- "good enough" version
- recovery/admin task
- follow-up task with a date

Avoid moralizing, scolding, or productivity theater. The plan should create motion, not shame.

Use Kaizen: improve the system by one small adjustment at a time. Do not rebuild the user's entire productivity setup when one checklist, label, reminder, or wording change would reduce friction.

## Justin Sung-Inspired Leverage Checks

Use these checks before adding tasks:
- Pareto: Which small subset of tasks produces most of the outcome?
- Bottleneck: Which task, if done, makes the rest easier or unnecessary?
- One-key-task focus: What deserves undivided attention today?
- Task management over time management: Is the task itself clarified enough, or are we only moving time blocks around?
- Parkinson's Law: What constraint would prevent this task from expanding?
- Activity vs outcome: Is this task a proxy for progress, or does it directly create the result?

## Todoist Creation Patterns

Use structured add when fields are obvious:

```json
{"args":["task","add","Draft rough outline","--due","tomorrow","--priority","p2","--json"]}
```

Use quick add when natural language is clearer:

```json
{"args":["task","quickadd","Draft rough outline tomorrow p2 #Writing","--json"]}
```

Batch conceptually, but call the tool safely. If creating multiple tasks, keep each command simple and report what was created.

## Output Shape

When planning without writing:
- Eisenhower matrix summary
- High-leverage 20% callout
- Weekly or tomorrow anchors
- Small next actions
- Waiting/delegate/batch items
- Parking lot/drop list
- One Kaizen improvement

When writing to Todoist:
- State the created tasks, due dates, and priorities.
- State anything intentionally left uncreated.
- Offer the next single action.

## Optional Templates

Use `assets/templates/weekly-plan.md` for weekly planning sessions.
Use `assets/templates/nightly-plan.md` for next-day planning.
Use `assets/templates/capture-clarify.md` when the user needs to empty and clarify a messy task pile before scheduling.

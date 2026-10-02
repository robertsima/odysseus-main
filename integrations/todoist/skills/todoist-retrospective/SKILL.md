---
name: todoist-retrospective
description: Reviewing Todoist and recovering momentum: daily or weekly retrospective, overdue cleanup, task triage, being stuck or overloaded, deciding what to do next.
---

# Todoist retrospective

Turn Todoist state into a compassionate review and one next step. The aim is learning, values alignment and momentum, with no blame. Call Todoist through `mcp__todoist__todoist`; command shapes and CLI facts are in [references/todoist-odysseus.md](references/todoist-odysseus.md).

## Workflow

1. **Pull the current picture** with JSON output: `{"args":["today","--json"]}`, `upcoming`, `inbox`, `task list`.
2. **Sort with the Eisenhower Matrix, without judgment:**
   - urgent + important: still needs direct attention;
   - important + not urgent: protect or reschedule on purpose;
   - urgent + not important: delegate, batch, automate or time-box;
   - not urgent + not important: drop, park or ignore.

   Also flag overdue or stale, waiting or blocked, unclear or oversized, and probably-not-worth-doing items.
3. **Find friction**: vague verbs, missing next actions, too many priorities, due dates used as guilt, tasks hiding several steps.
4. **Review leverage with the checks below**, treating busyness as data and not as achievement.
5. **Connect to values.** When the list feels arbitrary, ask what the user is trying to protect or become, and treat the task list as the bridge from values to action.
6. **Recommend one reset**:
   - complete, delete or reschedule stale tasks;
   - rewrite vague tasks into next actions;
   - choose a minimum viable day;
   - create a waiting-for follow-up;
   - park nonessential work.
7. **Propose changes before making them.** Completing, deleting, renaming, rescheduling or moving existing tasks changes the user's system, so act only when the user clearly asked you to.

## Leverage checks

Justin Sung-style: judge by outcome, not activity.
- Pareto: which few actions created most of the progress?
- Anti-Pareto: which few sources created most friction, procrastination or rework?
- Parkinson's Law: which tasks expanded because they had no constraint?
- One key task: did the day or week have a clear primary task?
- Task clarity: were tasks actionable, or did vague wording create avoidance?
- Outcome over activity: what should be measured by result and not time spent?

## Cadence

Weekly retrospective asks:
- What mattered this week?
- Which important but not urgent work got protected?
- Which urgent but not important work hijacked attention?
- Which 20% of actions produced most of the meaningful results?
- Which recurring activities looked productive but moved no outcome?
- What should be delegated, batched, deleted or moved to someday?

Nightly review asks:
- What is still open from today?
- What deserves forgiveness, rescheduling or deletion?
- What was the one key task, and did it get real attention?
- What is the first task for tomorrow?
- What can be made smaller before sleep?

Add prompts from this set when they help: What actually moved? What kept showing up? What was blocked by missing information, energy or a decision? What is the next kind action? What constraint would have stopped today's work expanding?

End every review with one Kaizen improvement, a small workflow change such as rewriting one recurring task, adding one checklist, removing one stale due date or protecting one deep-work block.

## Executive dysfunction mode

When the user sounds stuck, scattered, ashamed or overloaded:
- Start with validation and a concrete next move.
- Offer 2-3 options.
- Prefer "open the document" or "send one sentence" tasks.
- Offer timers gently.
- Turn "catch up on everything" into a short rescue plan.
- Keep it short and direct, and make ambiguity smaller before prioritizing ("just prioritize" does not help).
- Do not create many new tasks out of anxiety: the rescue plan is the only addition.

## Updating Todoist

After consent only:

```json
{"args":["task","complete","<task-id>","--json"]}
```

```json
{"args":["task","update","<task-id>","--due","tomorrow","--json"]}
```

For unclear inbox items, propose rewrites first. Once accepted, update the existing task so it is not duplicated.

## Output shape

Short enough to act on immediately:
- what I see
- Eisenhower readout
- leverage readout
- what to keep
- what to change
- suggested Todoist edits
- one Kaizen improvement
- one next action

## Templates

Offer them when the user wants a reusable artifact: `assets/templates/weekly-retrospective.md`, `assets/templates/nightly-review.md`, `assets/templates/stuckness-reset.md` (overloaded, avoidant or recovering momentum).

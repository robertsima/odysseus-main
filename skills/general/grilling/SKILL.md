---
name: grilling
description: "Grill the user relentlessly about a plan, decision, or idea, one round of numbered questions at a time with a recommended answer for each. Use when the user wants to stress-test their thinking or says grill me, interview me, or poke holes in this."
version: 1.0.0
category: general
tags: [grilling, interview, planning, decision-making]
platforms: [linux, windows, macos]
status: published
source: bundled
---

Interview the user relentlessly until you reach a shared understanding. Map this as a **design tree**: every decision branches into the decisions that hang off it.

Work the tree in **rounds**. The **frontier** is every decision whose prerequisites are already settled: the questions you can ask _now_ without guessing at answers you haven't heard yet. Ask the whole frontier in one round: number each question and give your recommended answer. Then wait for the user's answers before the next round.

Format a round like so:

```
❓ **Q1** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>

---

❓ **Q2** - **<question title>**: <question body, might be multiple paragraphs, including multiple choices>

➡️ <your recommended answer>
```

Each round the user answers reshapes the tree: settled decisions push the frontier outward and unblock questions that depended on them. Recompute the frontier and ask the next round. A question whose answer depends on another question still open in this round belongs to a _later_ round, not this one.

Finding _facts_ is your job, never the user's. When a frontier question needs a fact from the environment (filesystem, tools, etc.), look it up yourself (`read_file`, `grep`, `glob`, `bash`, `web_search`) or hand it to a delegated agent; don't ask the user for anything you could look up yourself. Don't block on it: a lookup in progress is an unsettled prerequisite, so only the questions downstream of it wait for that lookup to finish; ask the rest of the frontier now. The _decisions_ are the user's: put each to them and wait.

The session is done when the frontier is empty: every branch of the design tree visited, nothing left silently assumed. Do not act on it until the user confirms you have reached a shared understanding.

---

## Provenance

- Source: https://github.com/mattpocock/skills/blob/d81f3a183412e71a5b1e84ca21bc1a35eea03a60/skills/productivity/grilling/SKILL.md
- Commit: d81f3a183412e71a5b1e84ca21bc1a35eea03a60
- License: MIT, Copyright (c) 2026 Matt Pocock
- Adapted for Odysseus: fact-finding names Odysseus tools instead of a Claude Code sub-agent; frontmatter converted. Post each numbered round as a normal message (`ask_user` only when a structured prompt helps).

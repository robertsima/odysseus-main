---
name: resolving-merge-conflicts
description: "Resolve an in-progress git merge or rebase conflict: understand each side's intent from history, resolve every hunk preserving both intents, run the project's checks, finish the merge or rebase. Use when a merge, rebase, cherry-pick or pull stops on conflicts."
version: 1.0.0
category: dev
tags: [git, merge, rebase, conflicts]
platforms: [linux, windows, macos]
status: published
source: bundled
---

1. **See the current state** of the merge/rebase. Check git history (`manage_git` status, diff, log), and the conflicting files.

2. **Find the primary sources** for each conflict. Understand deeply why each change was made, and what the original intent was. Read the commit messages, check the PRs, check original issues/tickets.

3. **Resolve each hunk.** Preserve both intents where possible. Where incompatible, pick the one matching the merge's stated goal and note the trade-off. Do **not** invent new behaviour. Always resolve; never `--abort`.

4. Discover the project's **automated checks** and run them, typically typecheck, then tests, then format. Fix anything the merge broke.

5. **Finish the merge/rebase.** Stage everything and commit. If rebasing, continue the rebase process until all commits are rebased.

---

## Provenance

- Source: https://github.com/mattpocock/skills/blob/153fc1b93de6584562765cdce299324e1ff9e661/skills/engineering/resolving-merge-conflicts/SKILL.md
- Commit: 153fc1b93de6584562765cdce299324e1ff9e661 (last commit before upstream removed the skill in daa01d8a)
- License: MIT, Copyright (c) 2026 Matt Pocock
- Adapted for Odysseus: frontmatter converted; git operations point at `manage_git`. Upstream has since retired this skill; the text is kept as it stood.

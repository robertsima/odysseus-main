---
name: local-pi-delegation
description: Delegating bounded coding work to the local Windows Pi/Qwen worker (cheap implementation, 32K context), then verifying it and recording acceptance in the owner's delegation log when one is kept.
metadata:
  version: 1.0.0
  category: dev
  status: published
  source: user
---

# Local Pi delegation

You decide architecture, risk, scope and acceptance. The local worker is a focused implementer and never the final reviewer.

## Choose the worker

Delegate work with explicit acceptance criteria and a small, discoverable file set: bug fixes, small features, targeted tests, configuration changes, documentation edits, mechanical refactors.

Keep work in the primary harness when it needs broad multi-repository context, an architecture choice, an ambiguous product decision, security judgment, a data migration, a destructive operation or sustained reasoning across many subsystems. [worker-profile.md](references/worker-profile.md) has the sizing detail and the tool inputs.

## Delegation loop

1. Decide the desired change, risks and acceptance criteria.
2. Build a payload under about 6K tokens (the worker's context ceiling is 32K) with:
   - the absolute project path from workspace discovery (the worker may need a Windows drive path, which you take as returned and never translate), inside the worker's configured root (`ODYSSEUS_PI_WORKER_ROOT`) and confirmed accessible to the worker;
   - one objective;
   - likely files, symbols or failing tests, as paths and names rather than whole files or conversation history;
   - constraints and exact verification commands;
   - an instruction to preserve unrelated worktree changes and report overlaps.
   Credentials, private keys, tokens and unnecessary personal data stay out of it. Independent changes go in separate calls.
3. Call `mcp__pi_worker__run_pi_task`, asking Pi to inspect before editing, implement only the requested change, run focused checks, and report files changed plus evidence. Commits, pushes, deployments, dependency upgrades and destructive commands need the user's explicit request.
4. Inspect the diff and test evidence yourself, and run local verification when available.
5. Allow one narrow repair or verification pass. After two unsuccessful passes, take the work over in the primary harness or escalate.
6. Accept only when every item below holds:
   - the diff matches the requested scope, with no unexpected files changed;
   - the stated tests, linters, builds or manual checks passed, and failures and limitations are stated honestly;
   - the result does not depend on hidden session context.
7. After acceptance, call `mcp__pi_worker__record_pi_task` with a concise outcome, changed files, verification and limitations, following [documentation-policy.md](references/documentation-policy.md). If the tool reports that no documentation root is configured, the owner keeps no delegation log: skip the record. Vault documentation is written by that record tool, never by the worker.

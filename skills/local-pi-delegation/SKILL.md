---
name: local-pi-delegation
description: Delegate bounded coding work to the local Windows Pi/Qwen worker when the task benefits from inexpensive implementation without a large context window; verify the result and record accepted changes in AI Mind.
metadata:
  version: 1.0.0
  category: dev
  status: published
  source: user
---

# Local Pi Delegation

Use the primary harness to decide architecture, risk, scope, and acceptance. Use the local worker as a focused implementation agent, not as the final reviewer.

## Choose the Worker Deliberately

Delegate bounded bug fixes, small features, targeted tests, configuration changes, documentation edits, and mechanical refactors. Prefer tasks with explicit acceptance criteria and a small, discoverable file set.

Keep work in the primary harness when it requires broad multi-repository context, architecture selection, ambiguous product decisions, security-sensitive judgment, data migrations, destructive operations, or sustained reasoning across many subsystems. Read [worker-profile.md](references/worker-profile.md) when sizing an assignment.

## Run the Delegation Loop

1. Determine the desired change, risks, and acceptance criteria before delegation.
2. Build a compact task payload containing:
   - absolute project path under `D:/Development`;
   - one objective;
   - likely files, symbols, or failing tests;
   - constraints and exact verification commands;
   - an instruction to preserve unrelated worktree changes.
3. Call `mcp__pi_worker__run_pi_task`. Ask Pi to inspect before editing, implement only the requested change, run focused checks, and report files changed plus evidence.
4. Independently inspect the diff and test evidence. Run local verification when available.
5. If needed, send one narrow repair or verification pass. After two unsuccessful passes, stop delegating and handle or escalate the work in the primary harness.
6. Accept only when the stated criteria pass and no material regression or scope drift remains.
7. After acceptance, call `mcp__pi_worker__record_pi_task` with a concise outcome, changed files, verification, and limitations. Follow [documentation-policy.md](references/documentation-policy.md).

## Protect Context and State

- Send paths, symbols, errors, and acceptance criteria instead of whole files or conversation history.
- Keep a task comfortably below the worker's 32K context ceiling; aim for a payload under roughly 6K tokens.
- Split independent changes into separate calls. Avoid asking one session to retain unrelated project history.
- Never include credentials, private keys, tokens, or unnecessary personal data.
- Do not authorize commits, pushes, deployments, dependency upgrades, or destructive commands unless the user explicitly requested them.
- Treat the repository as potentially dirty. Preserve unrelated edits and report overlaps instead of overwriting them.
- The worker may edit only projects below `D:/Development`. Vault documentation is performed by the Odysseus-side record tool after verification.

## Verify Sufficiency

Before recording acceptance, confirm:

- the diff matches the requested scope;
- relevant tests, linters, builds, or manual checks passed;
- reported file paths exist and unexpected files were not changed;
- failures and limitations are stated honestly;
- the result does not depend on hidden session context;
- the documentation entry is concise and contains no chain-of-thought, secrets, large diffs, or raw logs.

# Publication identity and local diagnostics

## Request and evidence

Robert wants coding agents to finish work without repeated workspace/settings
intervention, while keeping real product feedback and publication approval.

The source incident is session `0b99366d-343f-418f-bb43-6b37fefa950a`.
Indexed chat search found the stopped delegation in `vault-pr60-rebase` and the
user's follow-up, "you have access to git just do it". The log trace includes a
180-second read-only delegation timeout and multiple completed reviews/test
runs. This is evidence of repeated execution/handoff overhead, not proof that
credentials were wrong.

The parent verified that the separate clone `vault-pr60-rebase` held tested merge
`5dfc9b4`, while registered `vault-user-doc-save` remained at `8cdab68`.
Different object stores meant the tested merge was not available for managed
publication. The parent recovered with a local object import and non-FF merge,
verified equal trees, and committed `267c490f14f042643af84c6aa9fb8eb97737b715`.
This feature must not touch that worktree or request
`ed3c2687eb154e0ea7856561ded1b5f4`.

Code inspection found three linked factors:

* Named `status` still scanned every worktree, even on `start`, and returned all
  projects. Agents got large inventories instead of a compact execution record.
* Managed path inference handled `_repos/<key>/<leaf>` but not the configured
  source repository's direct `<root>/<leaf>` layout.
* Publication froze the registered HEAD but accepted no tested HEAD receipt.
  A correct approval gate could therefore freeze the wrong locally reviewed
  revision after a clone-based handoff.

`shell_sandbox.linked_git_binds` already validates back-pointers and binds the
shared Git directory. No broader mount permissions are needed. If shell Git
fails, the host-side diagnostic can distinguish valid registered metadata from
a genuinely invalid pointer before agents request settings changes.

## Acceptance criteria

1. `request_publish(expected_head=<full tested SHA>)` refuses any different
   registered HEAD before credentials, network or approval state. Omitting the
   field keeps API compatibility. Runtime coding guidance tells agents to use it.
2. `diagnose` checks one registered worktree read-only, with no credentials or
   remote requests. It returns repository, branch, path, actual HEAD, dirty state,
   shared object-store path and tested-object availability when supplied.
3. An absent tested object and different HEAD report `OBJECT_STORE_DRIFT`.
   Invalid metadata reports `WORKTREE_METADATA_INVALID` with an explicit host
   repair boundary. Neither case overwrites files, refs, pointers or history.
4. Managed worktree paths in either layout route to their verified main
   repository and inferred branch. Named status returns only that worktree.
5. Global status returns at most 50 entries and an omitted count. Named status
   executes three local Git commands regardless of unrelated worktree count.
6. Matching receipts still produce a pending human approval request. Existing
   publish re-verification, sensitive-path checks and credential gates remain.

## Agent flow

Keep the returned main repository, managed path, branch, base and tested HEAD in
the handoff. Work and test in that path. If shell metadata appears inaccessible,
call `diagnose` with the main repository and branch instead of cloning or asking
the user to invent worktree paths. A healthy host result allows existing managed
status/diff/commit operations; it is not a claim that shell mounts are healthy.

Request publication with the tested HEAD. On mismatch, the tool returns the
complete diagnostic call including the same receipt. Preserve both histories
and use an approved local integration route if one is available. Recheck tree
and test evidence after integration. If no supported local import route exists,
report that precise boundary rather than credentials or permission guesses.

Automatic clone object import is not part of this feature. It needs its own
approved-source validation, history-preserving integration and conflict policy.
Diagnostics do not approve, push, merge, reset or silently repair metadata.

## Verification and status

Status: implemented, checked and independently reviewed; awaiting parent review
before any publication request. No publication, merge or deployment performed.

The first regression run against `origin/dev` failed all three initial tests:
the API rejected `expected_head`, named status included an unrelated worktree,
and no diagnostic action existed. The real temporary-repository replay creates
a separate clone with a newer commit absent from the registered object store.

Focused worktree tests passed 46 tests after the first implementation. The
affected lane then passed 2082 tests and caught two over-budget parameter
descriptions; those descriptions were shortened before the final checks.

The operation-count regression asserts three local Git calls for named status
with four worktrees. Previously the same path read each worktree's branch, HEAD
and dirty state, then reread the target HEAD and dirty state. Removing that scan
reduces local subprocess work and output, but makes no measured wall-clock or
model-round speed claim. `diagnose` uses three Git commands, plus one `cat-file`
when a receipt is supplied. It avoids a network/credential probe for this class
of failure. The runtime guidance adds about 600 characters to coding context.

AI Mind write and Todoist task tools are denied by this worker's capability
profile. This checked-in spec is the pending record for the parent to save via
supported indexed/document routes. Suggested backlog: evaluate a confined,
history-preserving local object-import operation; do not grant blanket host
shell access as a workaround.

Independent review identified and regression tests reproduced two edge cases:
Git's promisor lazy-fetch imported a supposedly absent receipt, and a deleted
pointer suggested `start`. Diagnostic Git now disables lazy fetch, allows no
transport and disables optional index locks. Existing paths with missing
metadata go through verification rather than recreation. No shell permission
expansion was needed.

Final checks after those repairs:

* 62 focused Python tests passed, including the real promisor and deleted-pointer
  regressions, publication/approval flow, multi-repository routing and schema
  budget (6.39 seconds on this runner).
* `python -m tests.run full` ran the Python full lane successfully: the runner
  reached its Node phase, which runs only when pytest returns zero, and pytest's
  failure cache is empty. The Python summary was displaced by the subsequent
  verbose Node errors in the terminal buffer, so no exact pass count is claimed.
* That first Node phase failed because this fresh worktree lacked `happy-dom`.
  `npm ci --ignore-scripts` installed the existing lockfile without modifying
  dependency versions. Only the failed Node phase was rerun, using the lane's
  file list, timeout and four-process concurrency: 506 passed, zero failed,
  two TODO, across 126 files (76.97 seconds).
* All 334 tracked JavaScript files passed `node --check`; changed Python files
  passed `compileall`; `git diff --check` passed.
* The read-only reviewer inspected the final diff and both repairs, found no
  remaining reproducible correctness/security defects, and ran diff whitespace
  checking. Its Python test assessment used the implementer's evidence, not a
  redundant suite run.

No UI code changed, so screenshots/browser renders are not applicable. These
are real local-Git integration checks with remote publication/credential calls
stubbed in approval tests; live GitHub publication was deliberately not tested.

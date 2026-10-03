# Test suite restructure, 2026-10-03

This plan came out of a design review of the Odysseus test suite on 2026-10-03. The owner asked three things: whether the suite has too many tests, why a full run takes so long, and whether the tests catch real bugs. Four measurements answered them:

- a census of every test;
- the CI history since 2026-08-12;
- a timed run with coverage on Linux;
- a mutation probe that planted 13 realistic bugs and 7 harmless refactors.

The numbers are in [Evidence](#evidence). The owner settled the decisions (D1 to D29) one round at a time. The phases at the end are the work order. Nothing in this document is implemented yet.

## Short answers

**Too many tests?** The count is not the problem. The suite has 11,354 collected tests, ten times the "over 1000" the owner expected. But 8,418 of them finish in under 10 ms each and take 46 s in total.

**Why so slow?** A few hundred tests take almost all the time:

- On CI, 143 browser tests in one file take about 590 s of a 1,028 s run. Every page load in them ends with `time.sleep(2.0)`.
- 73 files build a file-backed SQLite schema per test or per module. On a disk with slow fsync, such as WSL's (and probably the NAS where agents run tests), that is 47% of all test time.
- 13 tests wait 10 s each for DNS to fail on made-up hostnames.

**Effective?** Partly. The probe's 13 planted bugs split like this:

- **Caught (5):** all by tests that run the real code.
- **Missed (8):** five of them in security checks.
- **Text pins:** tests that read source files as text caught none of the 13.
- **False alarms:** two of the seven harmless refactors broke five tests between them.

CI history tells the same story. Of about 200 tests that went red between 09-18 and 10-03, 22 caught real regressions; the rest failed because of state leaking between tests or pins on wording.

## Findings

Speed:

- **Where CI's time goes.** The serial pytest step went from about 400 s on 10-01 to about 1,000 s on 10-03, mostly from the browser tests. CI's 1,028 s split as 34 s collection, about 590 s browser, about 405 s for the other 11,000 tests.
- **Collection.** 25 files build a database schema at import time, so collection takes two to three minutes in WSL. Under xdist every worker collects again.
- **Disk.** The schema-building files create temp databases with `NamedTemporaryFile(delete=False)` and never delete them. WSL's `/tmp` held over 14,000 of them.
- **Concentration.** The slowest 1% of tests (114) take 61.6% of the time, and the slowest 5% take 91%.

Effectiveness:

- **Red dev.** Dev was red 81% of the time between 09-18 and 10-03. No check is required on dev, and from 10-01 every red dev run had been red on its PR before the merge.
- **Why tests went red.** About 200 distinct tests failed in that window. 22 caught real regressions. About 100 failed because another test left state behind. About 67 pinned wording or structure that had changed on purpose.
- **Text pins.** 627 test functions (6.8%) read production source as text and assert substrings. Half of all frontend tests work this way, and 76 of 183 frontend modules have no test at all.
- **Route tests.** 1,349 tests call route functions directly with fake requests, which skips FastAPI's auth dependencies. Only 132 go through HTTP. Route coverage is 46.6%, against about 75% for `src`, `services` and `core`.
- **Mocks.** Some tests replace the very function they are named after. `test_list_sessions_excludes_other_users_sessions` mocks `get_sessions_for_user` to return only Alice's session, so it passes when the real owner filter is deleted (mutant B01).
- **Security misses.** Of the 8 planted bugs in security checks, the suite caught 3: the SSRF guard, the vault sensitivity filter and the worker authority cap. It missed 5:
  - session-list owner scope;
  - the shell routes' admin check;
  - workspace confinement against a sibling folder that shares the workspace's name prefix;
  - approval for risky shell commands that do not start at position 0, such as `cd x && git push`;
  - event-handler attributes in the markdown sanitizer.
- **Dead tests.** 206 test functions sit in eight module-skipped "re-port backlog" files and never run.

Growth:

- **Pace.** 341 test files were added in the four weeks before this review, and none were removed.
- **Who adds them.** In the last twelve weeks, 74% of new test files came from Claude Code sessions and 4% from in-app agents.
- **No rules.** No `AGENTS.md` or `CLAUDE.md` in the repo said anything about tests.

## Decisions

### Goals

- **D1. Priorities.** The security net (owner scope, auth, confinement, SSRF, sensitivity and approval gates) always runs in full. After that, the suite's job is the merge gate for agent-written changes, then fast feedback while building. Documentation is a by-product.
- **D2. Time budgets.**
  - An agent checking its own change gets an answer in under 60 s.
  - The full check before a hand-back or push takes under 3 min.
  - A CI verdict takes under 6 min wall clock.
  - A nightly tier has no time limit, but its failures open an issue.
- **D3. Linux decides.** CI on Linux is the judge. Tests that cannot run on Windows get an explicit skip with a reason. A Windows run is then green but partial, instead of showing about 151 known failures.
- **D4. The fork owns `tests/`.** On an upstream sync, take upstream's production code. Adopt upstream's tests one at a time into this layout, or drop them.

### What a test must be

- **D5. The keep bar.** A test stays only if you can name a realistic production bug that it catches and no other test catches. Tests that fail only when wording, formatting or file layout change get rewritten as behavior tests or deleted.
- **D6. Red evidence.** A bug-fix or feature PR's new tests must fail on the base commit and pass on the head commit. CI checks this (D17).
- **D10. Frontend logic** is tested with `node --test`, a small shared DOM library (happy-dom or linkedom as a dev dependency) and one shared `fetch` fake. Tests import the real modules and never slice source text into node.
- **D23. Route tests go through HTTP.** They use a shared fixture that provides two logged-in users and an admin. Existing direct-call tests of auth, admin or owner-scope behavior convert first. The rest convert when someone touches them.

### Where tests live and who reads the rules

- **D11. The test tree mirrors the production tree.**
  - Tests for `src/agent_loop.py` live under `tests/src/agent_loop/`, route tests under `tests/routes/...`, and frontend tests under `tests/static/js/...`.
  - Kind, speed and security are markers, not folders.
  - One-file-per-bug files merge into their module's folder.
- **D14. The rules live in a repo `AGENTS.md`**, which `CLAUDE.md` imports.
  - It carries the rules that change behavior: where a test goes, the keep bar, red evidence, and which command to run when.
  - `tests/TESTING_STANDARD.md` keeps the detail and is rewritten to match this plan.
  - The in-app agent prompt points to the same file.
- **D13. Dead backlog tests are deleted.** This covers the eight module-skipped files and the individually skipped "Re-port backlog" tests. `website/upstream-sync-2026-09-18.md` records the last commit of each. The PR that re-ports a feature restores its test with `git show <commit>:<path>`.

### Isolation and speed

- **D12. A guard test bans patterns that leak state between tests**: module-scope `sys.modules` assignment, `importlib.reload` of production modules, and raw `os.environ` writes. Existing offenders sit in an allowlist that may only shrink. The nightly run shuffles test order.
- **D21. The default suite is hermetic.**
  - A conftest guard makes DNS lookups and connections to non-loopback addresses fail at once. A marker lets a test opt out.
  - One shared fixture builds the database schema once per session and gives each test a fresh copy on tmpfs or in memory, deleted afterwards.
- **D19. CI runs parallel jobs.**
  - `python-tests` runs everything except browser and nightly tests with `-n auto`.
  - A separate `browser` job runs the browser tier.
  - The syntax and node jobs stay.
  - The main job is split into shards only if it passes 4 min.
- **D20. Browser tests use canonical widths.** They are ported with trimmed parametrization: 1440, 700 and 390 px. A test keeps another width only when it is about that breakpoint.

### Browser tier

- **D8. Browser tests run in their own parallel CI job on every PR**, with a budget of about 5 min. Besides the existing layout tests, a few flows run against the real backend with the mock model from `scripts/e2e_orchestration`:
  - log in;
  - send a chat and watch it stream;
  - an agent hand-back;
  - open a vault document;
  - save settings.
- **D9. One browser driver: Python Playwright** with system Chrome (`channel="chrome"`), so CI downloads no browser. The two files that use the raw CDP helper in `tests/helpers/agamemnon_browser.py` are ported, and the helper is deleted.

### Running tests

- **D22. One entrypoint, `python -m tests.run <lane>`**, replaces `tests/run_focus.py`:
  - `affected` runs the tests that cover the changed files (D15). It is the inner loop.
  - `full` runs everything except the browser and nightly markers with `-n auto`. Run it before a hand-back or a push.
  - `browser` runs the browser tier. Run it when `static/` or a page-serving route changed.
  - `security` runs the security-marked tests. `full` always includes them.

  On Windows, `full` hands off to WSL. `AGENTS.md` and the in-app agent prompt name the lanes. The in-app delegator keeps verifying a hand-back from its evidence.
- **D15. Affected-test selection uses a nightly coverage map.**
  - The nightly job records per-test coverage and publishes a map from production file to tests on a `test-map` branch.
  - `scripts/affected_tests.py <changed files>` reads it, and falls back to the mirrored folder for files the map has not seen.
  - It serves in-app agents, Claude Code and the report-only "focused test guidance" CI job.

### CI gates and the nightly tier

- **D7. A ruleset on dev requires the CI jobs to pass before a PR can merge.**
  - Direct pushes by the owner bypass it, including Claude Code sessions pushing as the owner. `AGENTS.md` tells those sessions to run `full` first.
  - The ruleset goes on only once CI fits the 6-minute budget.
- **D17. The red-evidence check.**
  - **Scope.** It applies to PRs that change `core/`, `src/`, `routes/`, `services/` or `static/`.
  - **Check.** It takes the PR's `tests/` folder and runs only the new or changed test functions against the base commit's production code. Each must fail or error on base and pass on head. An `ImportError` for a symbol the PR adds counts as red.
  - **Moved tests** are matched by function-body hash and ignored.
  - **Refactors.** A PR labelled `refactor`, with one line in its description saying why, skips the check.
  - **Rollout.** It runs report-only for about two weeks, then becomes required.
  - **Direct pushes** cannot be checked. `AGENTS.md` asks those sessions to paste the red run into the commit message or summary.
- **D16. A nightly tier on GitHub Actions** runs:
  - tests in random order;
  - the full set of real-backend browser flows;
  - the orchestration scenarios from `scripts/e2e_orchestration`, with bubblewrap installed on the runner;
  - the coverage-map build;
  - a `windows-latest` run;
  - the weekly mutation benchmark (D28).

  A failure opens or updates one issue labelled `nightly-red` that lists the failing tests.
- **D18. Coverage is measured nightly only.** It feeds the D15 map and a coverage trend. PRs carry no coverage step and no coverage gate.
- **D24. Coverage gaps get a targeted security backfill**: owner scope and auth on the document, session, gallery, shell, email and cookbook routes, tested through HTTP. Everything else fills in as it changes.

### Work order

- **D25. Phases** run in the order below. Each phase leaves dev green.
- **D26. Who does the work.**
  - Claude Code sessions build phases 1 to 3, because those change how everything else is checked.
  - In-app agents take the repetitive slices of phases 4 to 6, in PRs of three to six same-pattern files. The new guards, red evidence and the required CI check their work.

### After the mutation probe

- **D27. Close the security holes first.** Phase 1 gains a step: a behavior test for each missed security mutant, plus the agent loop's tool-result threading (B06). Each test is shown failing with its mutant patch applied. This step does not wait for the phase 6 backfill.
- **D28. The mutation probe becomes a benchmark.**
  - The 20 patches in [`testing-restructure-2026-10-03/mutants/`](testing-restructure-2026-10-03/mutants/) move to `tests/mutants/` with a runner script.
  - The nightly workflow runs them weekly. It reports a planted bug that stops being caught, a harmless refactor that starts failing tests, and a patch that no longer applies and needs a refresh.
  - Every real regression CI catches becomes a new planted bug.
- **D29. Cut rules for phase 4:**
  - **Source-text pins** (354 frontend, 199 Python, 19 that slice source and run it) are rewritten as behavior tests when they guard a bug you can name, and deleted otherwise. Security-related pins go first, such as `test_markdown_dom_xss_helpers.py` and the owner-scope pins.
  - **Prompt-wording pins** (88 functions) stay only for tokens that code or another model parses: the hand-back's `Task:` label, `Needs user:` lines and `[[no-update]]` markers. Pins on prose go.
  - **Config and CI file checks** (51 functions) stay when they guard a deployment property, such as compose security options. They go when they pin the layout of `ci.yml`, which D19 rewrites anyway.
  - **Docs and asset checks** (23 functions) stay. They are fast, and one caught a real content problem on 10-02.
  - **Mock-heavy tests** (102 functions where mock-call assertions dominate) are judged by the keep bar. A test that stubs the function it is named after gets rewritten.
  - **Browser tests** assert on roles, state, geometry and data attributes, not on visible label text, unless the label is what the test is about. Mutant N04 broke three browser tests by rewording "New chat".

## Phases

### Phase 1: stop the pile growing (Claude Code)

1. Add `AGENTS.md` with the testing rules and a `CLAUDE.md` that imports it. From this point, new test files go into the mirrored path, so the flat folder stops growing.
2. Make nested test folders safe to add, with package `__init__.py` files or `--import-mode=importlib`. Check the pass set is identical before and after.
3. Add the hygiene guard test (D12). It scans `tests/` with `ast` for the banned patterns and for new source-text assertions on production files. Today's offenders go in an allowlist with one reason each. The guard fails when a listed file no longer offends, so the list only shrinks.
4. Add the network guard (D21), and fix the 13 tests that resolve made-up hostnames.
5. Close the security holes (D27). For each item, write a behavior test and show it fails with the mutant patch applied:
   - Session-list owner scope (B01), through HTTP with two users. Rewrite `test_list_sessions_excludes_other_users_sessions`.
   - The shell routes' admin check (B02): a logged-in non-admin gets 403.
   - Workspace confinement (B03): a sibling folder whose name starts with the workspace name is refused.
   - Risky-command approval (B08): a risky command after `&&` or `;` still needs approval.
   - The markdown sanitizer (B13): `onerror` and `onclick` are stripped. Test it by running the sanitizer, not by reading its source.
   - Tool-result threading (B06): each tool result carries the `tool_call_id` of the call it answers, tested at the call site in the agent loop.
6. Add the red-evidence job to `ci.yml` in report-only mode (D17).
7. Clear out dead test files:
   - Delete the dead backlog tests and record their last commits in `website/upstream-sync-2026-09-18.md` (D13).
   - Delete the orphan `tests/markdown_codefence_placeholder_regression.mjs`, `tests/bombadil-spec.ts`, and the unused `@antithesishq/bombadil` dev dependency.
   - Remove the shadowed first copy of `test_custom_theme_and_accessibility_controls_remain_supported`.
8. Fix the known ordering leak in `test_worker_stop_and_reasoning.py`. It fails with "no such table: sessions" under xdist, as it did in 3 of the probe's 20 runs.
9. Add Windows skips with reasons for the node ESM path, POSIX path and stale-fixture failures (D3).
10. Rewrite `tests/TESTING_STANDARD.md` and `tests/README.md` to match this plan.

Done when:

- CI is green.
- A Windows run shows no failures.
- The guard rejects a sample change that adds a module-scope `sys.modules` stub.
- Each step-5 test fails under its mutant patch.

### Phase 2: speed (Claude Code)

1. Add the shared database fixture (D21). Move the 73 schema-building files onto it, and remove the 25 schema builds that run at import time.
2. Stop the autouse `_isolated_declared_tools` fixture in `tests/conftest.py` from requesting `tmp_path` for every test. Creating 11,000 numbered temp folders costs about 30 s in a serial run.
3. Shorten the tests with real waits, or mark them for the nightly tier:
   - `test_repository_listing_perf`, 12 s;
   - the tmux pane test, 10 s;
   - `test_auth_config_lock_concurrency`, 27 s.
4. Port the browser tests to Playwright with canonical widths (D9, D20), and add the real-backend flows (D8).
5. Add the node DOM library and the shared `fetch` fake (D10). Write executed tests for the two missed frontend bugs:
   - the segmenter must not close an outer fence on a shorter inner one (B10);
   - older-page loading must not duplicate the bubble at the page seam (B11).
6. Add the shared HTTP client fixture with two users and an admin (D23).
7. Split CI into the `python-tests` and `browser` jobs (D19).
8. Add `python -m tests.run` with its lanes (D22) and delete `tests/run_focus.py`. Change the test line in `_API_AGENT_RULES` in `src/agent_loop.py` to name the lanes.
9. Turn on the dev ruleset (D7) once the median push-to-verdict time over a week is under 6 min.

Done when, on GitHub's runners, `python-tests` takes under 4 min and `browser` takes under 5 min, and `full` takes under 3 min in WSL.

### Phase 3: nightly tier (Claude Code)

1. Add `.github/workflows/nightly.yml` with the D16 jobs.
2. Build the coverage map and publish it to the `test-map` branch. Add `scripts/affected_tests.py` (D15), and point the `affected` lane and the focused-test-guidance job at it.
3. Move the mutation patches to `tests/mutants/` and run them weekly (D28).
4. Open or update the `nightly-red` issue on failure.
5. After about two weeks of report-only results, make the red-evidence check required.

Done when:

- Three nightly runs in a row finish.
- `affected` picks the right tests for a change to `src/agent_loop.py` in under 60 s.

### Phase 4: cuts and rewrites (in-app agents)

Apply the D29 cut rules group by group, three to six same-pattern files per PR, in this order:

1. security-related source pins;
2. the other frontend pins, rewritten into the node tier or browser flows, or deleted;
3. Python source pins;
4. prompt-wording pins;
5. config and CI file checks;
6. mock-heavy tests;
7. the 21 node wrapper files that slice source text instead of importing the module.

Each deletion PR says which test still catches the bug the deleted test was meant to catch, or why that bug is not realistic.

### Phase 5: layout migration (in-app agents)

Move files into the mirrored tree (D11) as mechanical moves, with no assertion changes in the same PR. Merge one-file-per-bug files into their module's folder, and merge the 97 test names defined in more than one file.

Check each PR with an identical pass set before and after, using the count of passed tests plus function-body hashes (node ids change). Retire `tests/_taxonomy.py` and the `area_*`/`sub_*` markers once the tree replaces them.

### Phase 6: security backfill (in-app agents)

Convert the direct-call auth, admin and owner-scope tests to HTTP (D23). Backfill owner scope and auth, through HTTP, for the document, session, gallery, shell, email and cookbook routes (D24).

## How to tell it worked

Track these, not the test count:

- Median push-to-verdict time on CI (target under 6 min), and the share of time dev is red (target near zero once the ruleset is on).
- Stale-pin failures: CI failures fixed by editing a test to match an intentional change. Target near zero.
- Isolation failures: tests that pass alone and fail in the suite. The nightly randomized run should find them before CI does.
- The mutation benchmark (D28). Today the suite catches 5 of 13 planted bugs, and 2 of 7 harmless refactors cause 5 test failures between them.
- Test time inside agent deliveries, measured per phase as in the 2026-10-03 delivery-speed review.

## Evidence

The measurements ran on 2026-10-03 against dev at f2700767 and 45d16fa3, which differ in five test and CSS files. Their raw output lived in a session scratch folder and is not kept. The mutation patches are kept in [`testing-restructure-2026-10-03/mutants/`](testing-restructure-2026-10-03/mutants/), and they apply to dev at 2cb46742.

### Census

An `ast` classifier sorted each of the 9,184 test functions by what its body does (plus helpers it reaches). Spot checks of about 80 functions put each bucket at about ±10%.

| Kind | Functions | Share |
|---|---:|---:|
| Unit tests on real objects | 6,376 | 69.4% |
| Route code called directly, no HTTP | 1,349 | 14.7% |
| Frontend source-text pins | 354 | 3.9% |
| JS run in a node subprocess | 289 | 3.1% |
| Python source-text pins (`ast.parse`, `getsource`, `read_text`) | 199 | 2.2% |
| Git or script subprocess | 136 | 1.5% |
| HTTP through TestClient | 132 | 1.4% |
| Mock-heavy (mock-call assertions dominate) | 102 | 1.1% |
| Prompt-wording pins | 88 | 1.0% |
| Real browser | 66 | 0.7% |
| Config and CI file checks | 51 | 0.6% |
| Docs and asset checks | 23 | 0.3% |
| Source sliced and executed | 19 | 0.2% |

Other census numbers:

- **Files.** 1,139 test modules; 430 of them hold three or fewer tests. Tests total 192.7k lines, against 227.1k lines of production Python and 198.7k lines of frontend code.
- **Isolation hazards.** 31 files mutate `sys.modules` at module scope. 8 files reload production modules. 46 files set environment variables at module scope.

### CI history

All 197 runs of `ci.yml` from 2026-08-12 to 2026-10-03 (112 failed):

- **Timing.** The `python-tests` job had a median of 7.9 min and a p90 of 13.9 min. Over the last ten runs, push-to-verdict had a median of 14.5 min.
- **Docs-only skip.** It never fired.
- **The 198 skips on CI.** 184 are "Re-port backlog"; the rest need ripgrep, bwrap, Docker, libmagic, markitdown or Windows.
- **Failures from 09-18 to 10-03,** by distinct failing test and cause:
  - 22 real regressions, plus one commit with conflict markers;
  - about 100 isolation leaks;
  - about 67 stale pins;
  - 6 environment problems;
  - 5 flakes.
- **Branch protection.** Dev has none, and the one ruleset only blocks branch deletion.
- **Runner minutes.** About 20 per PR update. The repo is public, so standard runners cost nothing.

### Profile

WSL (16 cores, CI environment variables), at 45d16fa3:

| Run | Wall time |
|---|---:|
| Serial | 1,557 s (156 s collection) |
| `-n 7` | 325 to 473 s, depending on load from the mutation runs |
| GitHub CI, serial, same commit | 1,028 s |

Where the serial time goes:

- **Phases.** 633 s of the serial test time was setup, 754 s was test bodies, and 5 s was teardown.
- **Slow tests.** 132 tests over 5 s each took 69% of the time.
- **Fast tests.** 8,418 tests under 10 ms took 46 s in total.
- **Collection.** 85 of 95 profiled seconds were SQLite schema creation at import time, about 3.4 s per `create_all` on WSL's disk.
- **Estimate for CI.** `-n auto` on GitHub's 4-core runner would give 5.5 to 6.5 min for the whole suite, and roughly 2.5 to 3.5 min without the browser tests.

Line coverage:

| Package | Statements | Coverage |
|---|---:|---:|
| `src` | 63,508 | 75.7% |
| `routes` | 31,120 | 46.6% |
| `services` | 7,090 | 74.7% |
| `core` | 3,230 | 76.6% |
| Total | 104,948 | 67.0% |

Files over 300 statements and under 40% coverage:

- `routes/cookbook_routes.py` 11%
- `src/tools/cookbook.py` 14%
- `routes/document/document_routes.py` 19%
- `routes/shell_routes.py` 20%
- `routes/gallery/gallery_routes.py` 21%
- `src/ai_interaction.py` 27%
- `routes/session_routes.py` 33%
- `routes/contacts/contacts_routes.py` 34%
- `routes/codex_routes.py` 36%
- `routes/task/task_routes.py` 37%
- `routes/email_routes.py` 38%
- `routes/history/history_routes.py` 38%

### Mutation probe

Each patch was applied to f2700767, and the full suite ran at `-n 7` under CI environment variables. Newly failing tests were compared with two baseline runs. The browser files ran separately with a headless Chromium for every patch to static files.

| Id | Change | New failures | Result |
|---|---|---:|---|
| B01 | `core/session_manager.py`: `get_sessions_for_user` drops the owner filter | 0 | missed |
| B02 | `routes/shell_routes.py`: `_require_admin` drops the `is_admin` check | 0 | missed |
| B03 | `src/tool_execution.py`: confinement uses `startswith` instead of `commonpath` | 0 | missed |
| B04 | `src/outbound_fetch.py`: private IP literals no longer blocked | 1 | caught |
| B05 | `src/rag_vector.py`: the public-only sensitivity clause is dropped | 5 | caught |
| B06 | `src/agent_loop.py`: tool results threaded with the wrong `tool_call_id` | 0 | missed |
| B07 | `src/agent_loop.py`: wrap-up round `>=` becomes `>` | 2 | caught |
| B08 | `src/approval_modes.py`: risky-command regex `search` becomes `match` | 0 | missed |
| B09 | `src/agent_loadouts.py`: `cap_to_starter` skips named loadouts from a worker | 2 | caught |
| B10 | `static/js/streamingSegmenter.js`: a shorter fence closes a longer one | 0 | missed |
| B11 | `static/js/sessions.js`: older-page limit off by one, duplicate bubble | 0 | missed |
| B12 | `core/session_manager.py`: truncation deletes from `index+1` | 4 | caught |
| B13 | `static/js/markdown.js`: sanitizer `\|\|` becomes `&&`, event handlers survive | 0 | missed |
| N01 | rename `_ledger_budget_for_round` and its callers | 0 | clean |
| N02 | reformat two functions in `chatRenderer.js` | 0 | clean |
| N03 | rename the CSS class `session-item` everywhere | 0 | clean |
| N04 | reword "New chat" to "Start chat" in `index.html` | 3 (browser) | false alarm |
| N05 | move `is_cors_preflight` to a new module and import it back | 0 | clean |
| N06 | reword one rule in `_API_AGENT_RULES` | 0 | clean |
| N07 | prettier-style reformat of the sanitizer in `markdown.js` | 2 (text pins) | false alarm |

The misses share one pattern: the tests replace or skip the layer that has the bug.

- **B01:** the session-list tests mock the session manager.
- **B02:** no test calls the shell routes' admin check.
- **B03:** no test tries a sibling folder with a shared name prefix.
- **B06:** the tool-result threading tests check the helpers, not the call site.
- **B08:** every risky command in the approval tests starts at position 0.
- **B13 and N07:** in `markdown.js`, the real XSS bug passed, while the harmless reformat failed two tests that pin the exact source text around the check.

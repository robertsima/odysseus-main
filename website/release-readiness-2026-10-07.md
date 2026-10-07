# Release readiness review: 7 October 2026

## Decision

**Not yet cleared for public release.** This branch fixes confirmed Phalanx archive, font, branding and setup defects. It provides fresh-user and browser evidence, but does not claim that all integrations or every application surface have been exercised. The remaining release gates below require review before publication. No live user data, deployment, agent settings or loadouts were changed.

Source checkout: `/app/data/development/odysseus-main` (unrelated dirty work preserved). Review branch: `agent/odysseus/release-readiness`, worktree `/app/data/agent_worktrees/release-readiness`, base `origin/dev` at `c50e6511d91ac14438f2aa27219f541cdb8258a2`. The configured target was verified as `dev`, not assumed to be `main`. Final commit and capture directory are recorded in the handoff and capture manifest, rather than embedding a self-referential commit hash here.

## Criteria and evidence

| Requested criterion | Observed result | Sources and limits |
| --- | --- | --- |
| Phalanx reflects agents and their children | Fixed the confirmed archive defect. A parent and its five workers stay one unit. Archiving removes all six from the live overview; Archived contains all six with their lineage; restoring brings all six back. | `routes/agents_routes.py`, `static/js/agentsDashboard.js`; six real-database archive regressions and frontend confirmation tests. Real subprocess scenario at 1440 and 390 pixels records 6 → 0 → 6 → 6 rows. Child lineage was seeded in the isolated SQLite DB; no model delegation was executed. |
| Functionality, UI and harness reliability sweep | Tested fresh startup, account setup/login, empty session/memory/note/model APIs, agents archive/restore, theme/font selection and repository/config defaults. Broad regression lane and focused browser checks accompany this branch. **Coverage is partial**, not a certification of all integrations. | `tests.run full`, route regressions, browser font tests and `.visual-check` logs. See release gates for unavailable services and remaining risk. |
| Agamemnon theme and consistent fonts | Blank app uses `data-style=agamemnon` and Space Grotesk at 1440, 700 and 390 pixels. Login now shares the palette and font resolver. Explicit font selection overrides page-style font without silently resetting to Monospace. | `static/js/theme.js`, `static/style.css`, `static/agamemnon-mockup.css`, `static/agamemnon-critic-fixes.css`, `static/login.html`. Four real Chromium computed-style tests passed. This checks declared computed families, not every glyph fallback or canvas renderer. Code blocks remain deliberately monospace. |
| Rename old product references without changing CI image | Changed visible main/login/settings/chat, tool descriptions, reminders, TOTP enrolment, OpenRouter title, mainstream README/setup/feature guides and many server messages to Agamemnon. Removed obsolete README raster branding. Preserved upgrade-sensitive names. **The full repository rename is not complete.** | Remaining references include historical audit/theme reports, technical architecture prose and integration client material. Specialised finetuned-model prompts retain their trained strings deliberately. CI image names are unchanged. Retained compatibility identifiers are listed below; remaining current-product prose is not being called a compatibility requirement. |
| Blank slate: no personal notes, memory or skills | Fresh subprocess created an admin account in temporary scratch storage. Before fixture sessions: sessions=[], agents rows=[], profiles=[], memory=[], notes=[]; model endpoints API answered. Main panels rendered with no browser page errors. Default bundled/integration skills were seeded by normal startup, not copied from the operator's data. | `.visual-check/<commit>/manifest.json` contains API observations, real-app captures and startup log. No configured model was provisioned; task execution and chat responses are not proved by this test. ChromaDB was deliberately unavailable, exposing graceful degraded startup but not vector functionality. MCP and pollers were disabled to avoid contacting external integrations. |
| Decouple operator-specific setup | Pi worker no longer assumes `D:/Development`, a specific script location or a vault named AI Mind. Missing configuration is explicit. Default worktree base follows the selected repository's origin/HEAD or checked-out branch instead of assuming this project's dev. Env examples use neutral document and repository paths. | `mcp_servers/pi_worker_server.py`, `src/agent_worktree/service.py`, `src/settings.py`, `.env.example`; regression tests create repositories on main/trunk. No actual Windows SSH/Pi execution was available. Other bundled specialist skill recommendations still warrant a portability pass. |
| Reduce environment/integration duplication | `APP_PUBLIC_URL` is the shared default for Google email/calendar and MCP OAuth callbacks. Existing redirect-specific overrides remain valid. Claude callback URL defaults to this instance's internal address when token-file callback access is configured. GitHub API base derives from `GITHUB_HOST`, including Enterprise hosts, unless explicitly overridden. | `src/oauth_redirect.py`, `src/mcp_oauth.py`, `src/agent_tools/claude_code_tools.py`, `src/github_credentials.py`, `src/agent_worktree/config.py`, compose files and `.env.example`; URL/base/alias regressions. Real OAuth providers and Enterprise credentials were not contacted. |

## Confirmed defects and changes

### Archive previously promoted child workers

The client tree builder treated workers whose parent was missing as standalone roots. The old archive endpoint only flagged the parent, so five idle workers could appear as five new top-level agents. The backend now preserves persisted lineage in the overview and treats archive/restore as a unit operation. Archive checks every descendant for active chat, subprocess or queued CLI work and pending approvals. A failed strict lineage read blocks the archive instead of hiding unverified work. Owner scope applies to archive and restore, including after restart. Independently archived children are not blindly restored with an unrelated parent.

The Archive confirmation names the workers being archived. Archived children offer Restore rather than Archive. Tests cover refusal for active children, owner scope, restart and pre-existing parent-only archives. The UI distinguishes roots from nested workers; screenshots show the root Release lead with inactive branches, not six independent agents. Activity counts remain activity counts, not a count of configured loadouts. Loadouts are a separate tab.

**Remaining risk:** archive/restore still writes sessions individually and does not wrap the whole unit in a single database transaction. A write failure partway through could leave a partially archived unit. A new descendant could also start between the active-work check and the writes. These are code-review findings, not reproduced data-loss claims. Transactional unit updates and a launch/archive coordination boundary are recommended before treating this operation as atomic.

### Font resolution had conflicting defaults

Theme boot wrote Monospace inline, while the Agamemnon stylesheet pinned Space Grotesk on some elements. The default selection is now Theme default; one `--ui-font` resolver supplies general UI text. Login uses the same resolution rules and existing preferences. Users' saved explicit fonts, palettes and page styles are preserved rather than overwritten during an upgrade.

### Configuration repeated addresses and private conventions

A new main-only repository previously inherited `dev`; it now follows the repository default when no target was explicitly saved. Existing saved target branches remain honoured. Public URL and GitHub host derivation reduce redundant values while preserving integration-specific escape hatches. Pi worker paths and acceptance-note location are explicit operator configuration, not assumptions about one person's filesystem.

## Upgrade compatibility

The following remain intentionally stable during this release:

- Docker service/image identifiers, CI image names, repository URLs, existing script executable names and installation directory names. Renaming an executable or a repository URL requires an actual alias/artifact, not a textual replacement.
- `ODYSSEUS_*` environment names. `src/env_aliases.py` accepts `AGAMEMNON_*` at Python app/config startup; if both are present the new spelling wins and the conflict is logged without values. **Docker Compose still forwards the legacy names. Use the documented ODYSSEUS spelling in stock Compose until canonical forwarding is added.** Standalone non-Python tools are not automatically covered by this alias.
- `CLAUDE_CODE_ODYSSEUS_*` callback names, API tokens, protocol headers such as `X-Odysseus-Run-Id`, browser preference/storage keys, plugin globals and persisted integration IDs. Changing them without migration would strand existing settings or break clients.
- Reminder searches/clear actions recognise both `Reminder (Odysseus):` and `Reminder (Agamemnon):`. Runtime interruption classification recognises old and new scheduler messages.
- New TOTP enrolments use issuer Agamemnon; existing authenticator entries and secrets are not rewritten.

These exceptions do not justify leftover visible product prose. A remaining-brand inventory should distinguish protocol/storage identity, historical character names and product branding before completing the rename.

## Verification scenarios and captures

The untracked `.visual-check` directory holds logs and reproducible capture code. `capture.py` starts `python app.py` with isolated SQLite/data/cache paths, loopback bind, authentication enabled, no provisioned model, MCP disabled and in-process pollers/tasks off. It registers and logs in an admin using test-only credentials. Its temporary data is removed on shutdown. It never reads the source checkout's data folder.

Actual runtime captures at widths 1440, 700 and 390 pixels:

- `live-login-*.png`, `live-blank-*.png`;
- `live-phalanx-*.png`, `live-notes-*.png`, `live-tasks-*.png`, `live-calendar-*.png`;
- real backend parent/five-child fixture: `live-unit-before-{1440,390}.png`, `live-unit-archived-{1440,390}.png`.

Additional `fixture-phalanx-*.png` uses `tests.helpers.static_app`'s canned activity API to render active root/child distinction. It is not evidence of a working model or live agent run. The manifest marks the fixture and real-app scenarios separately. Screenshots opened with `preview_file` include desktop blank, mobile blank Phalanx, fixture desktop root/child tree, real desktop parent unit and real mobile archived unit. The mobile detail pane is scrollable; its bottom content is below the viewport. More keyboard/focus and deep-panel interaction coverage is still needed.

Focused evidence:

- Initial Python regressions: 21 passed, 4 browser skips (before installing Playwright).
- Final archive regressions: 6 passed, including strict-read refusal and active-child refusal.
- Chromium font/login tests: 4 passed using system `/usr/bin/chromium`, with explicit unauthenticated login-status fixture for the static login test.
- Node archive/font tests: 4 passed after waiting for asynchronous account-theme boot to settle. The initial font test failure was a test setup race, not accepted as a passing result.
- Changed JavaScript syntax checked with `node --check --input-type=module`; `git diff --check` passed.
- The first full lane exposed one outdated expected email brand. That expectation was updated to Agamemnon. A narrower agents suite exposed two mocks that did not accept strict keyword reads; those were corrected. Final lane results are in the handoff and `final-full.log`, not inferred from a delegated claim.
- Final `ODYSSEUS_TEST_WORKERS=auto python -m tests.run full`: Python **11,076 passed, 4 skipped, 3 xfailed**, then Node **520 passed, 0 failed, 2 TODO**; runner exit 0. Browser/nightly markers are excluded by this lane. The later documentation pass and one product-name correction in the normal agent rules do not change application control flow; their focused checks are recorded separately in the handoff.

No application build is required for this no-build browser frontend. `npm ci --prefer-offline` installed the pinned dependencies. Docker image build, Compose startup, real model execution, vector search, mail/calendar OAuth, Todoist, Penpot and Windows Pi were not checked against live services.

## Release gates and follow-up

1. Independent read-only review of this exact branch and final captures. No publish request has been created.
2. Complete the remaining user-facing brand/docs/default-skill pass while retaining explicit upgrade aliases. Main README and setup/feature guides now use Agamemnon; historical audits and technical interface names remain. The `_minimal_odysseus_*` prompts in `src/agent_loop.py` are for an existing finetuned model trained on those exact strings (documented in `website/prompt-audit-2026-10-01/A1-system-prompt.md`), so changing their identity text is not a safe blanket replacement. Preserve or explicitly version that compatibility path rather than silently changing it.
3. Make unit archive/restore transactional and assess launch/archive concurrency; test mid-write failure and workers starting during archive.
4. Validate a complete stock Compose fresh install with healthy ChromaDB and configured model. This review deliberately exercised the missing-vector degraded path; startup logs record that degradation.
5. Check onboarding with a first model and first useful task. The blank screen currently says New chat ready and offers Select model; it does not establish model-independent usefulness or a successful first answer. Default suggested skills must be checked for stale product names and private-use assumptions.
6. Add desktop/mobile interaction captures for Settings, Brain, skills, Workbench, attachments and other major surfaces; the current report is a representative sweep, not an exhaustive UI interaction audit.
   Fresh first-use hints can accumulate and obscure Calendar and Tasks: the desktop calendar capture shows both the Notes/Vault hint and the window-drag hint over the panel, and the mobile Tasks capture still carries the Notes hint. This is observed UI clutter, not a service failure. Hints should be scoped to their owning panel, dismissed when it closes, or presented one at a time. The blank screen's subordinate text also has low contrast; formal contrast ratios were not measured in this pass.
7. Validate real external integration setup and permission failures on a non-operator account. No credentials were requested or copied for this review.

# Release readiness review: 7 October 2026

## Decision

**Release remains blocked.** Confirmed archive and configuration defects are repaired. Local regressions and fresh-process browser contracts have observed evidence. A stock Docker/healthy-vector installation and credential-backed first model/external integrations are not verified. Remaining identifier migration/branding inventory is not complete. This report is not approval to publish or deploy.

Source: `/app/data/development/odysseus-main`. Worktree: `/app/data/agent_worktrees/release-readiness`, branch `agent/odysseus/release-readiness`, integration base `origin/dev` at `c50e6511d91ac14438f2aa27219f541cdb8258a2`. Dirty source work was preserved. Final SHA and capture provenance are in the handoff and `.visual-check/<SHA>/manifest.json`; they cannot be embedded here without changing that SHA.

## Criterion outcomes

| Criterion | Outcome and evidence |
|---|---|
| Phalanx represents agents coherently | Repaired root-only archive, which exposed children as roots. UI shows roots and children and archives/restores units. Real backend capture checks six live rows become zero live rows, six archived rows and six restored rows. `test_archive_takes_the_unit.py` and `test_unit_archive_atomic.py` cover restart lineage, pending approvals after finished turns, transactional write rollback, nested independently archived units and concurrent worker launch. |
| Functionality, UI and reliability | Representative fresh-account panels, keyboard activation/focus, attachments, login and streaming chat are checked. Existing broader regression lane plus focused browser contracts cover owner isolation and error paths. One pre-existing strict browser xfail remains: nested Markdown fences render incorrectly (`tests/static/index/test_live_chat.py:72`). This is a release issue, not a green browser result. |
| Agamemnon theme and fonts | New account defaults to Agamemnon/Space Grotesk. Login, composer, Brain/Skills, Workbench and Phalanx use the selected UI font. Saved explicit preferences remain supported. Chromium tests cover 1440/390 widths; captures add 700. Website palette and product title updated. Desktop Tk launcher text/palette updated; native launcher rendering is not available in this headless Linux runtime. |
| Rename product references | Main UI, agent identity/domain prompts, reminder persona labels, launcher, generated research reports, primary guides and Claude integration instructions updated. Historical evidence, remote repository paths and persisted identifiers still contain old strings. Canonical env/header aliases are implemented for principal runtime boundaries, but the complete plugin/executable/browser-key migration inventory remains open. CI deployment image name is unchanged. |
| Blank slate | Real subprocess uses fresh temporary app data, no personal notes/memory/sessions/loadouts. Normal seeded suggested skills remain. Blank API responses/startup are checked. Separate fresh subprocess provisions a local scripted OpenAI-compatible endpoint and verifies streaming/saved reply and worker handback through browser tests. This proves the harness contract, not model quality or live provider access. Healthy ChromaDB and stock Compose are blocked by unavailable Docker/server runtime. |
| Decouple operator-specific setup | Pi worker root/script/acceptance-note paths are operator settings, not `D:/Development`/AI Mind assumptions. Repository integration base follows discovered default/main/trunk when unsaved, not `dev`. Built-in skills were scanned for Robert/AI Mind/Vault Mind/hardcoded development paths; no such defaults found. Historical diagnostic anecdotes are not configuration or blank-slate proof. Windows Pi needs an operator-provided SSH host and installation paths; no Windows host was accessed. |
| Environment cognitive complexity | Shared Public URL defaults for OAuth, derived GitHub API base, canonical env aliases and Compose forwarding reduce duplicate address settings. MCP provider creation resolves saved Public URL at request time instead of freezing import-time env. Invalid GitHub host raises before credential routing, never falls back to public GitHub. Contract regressions verify all legacy Compose env names have canonical forwarding. |

## Archive safety boundary

`core.database.archive_session_unit` writes archive flags and `archived_with` settings in one transaction. A failed flush rolls back every row and no cached flag is changed. Missing/foreign rows and malformed settings fail closed. Independently archived children retain their earlier restore marker and are not taken by a later ancestor archive. Persisted restore was already transactional through `unarchive_sessions`; the earlier report incorrectly described restore as individual commits.

`src.agent_lifecycle.unit_lock` coordinates managed worker creation/registration, send-to-session child creation and busy registration, archive validation/commit, and restore. It is a process lock, consistent with the documented single-uvicorn-worker deployment; **multi-process deployment is not supported by this change**. No external model/tool await is held inside the critical sections. Archive checks `tool_approval_store.has_pending_for_session` for every target, not only activity telemetry. Pending expired/retired decisions follow the real approval store's cleanup semantics. Strict lineage read failure refuses the operation.

Root/child grouping uses durable `parent_session` links in addition to live worker metadata. Archived intermediates are included when discovering nested descendants after restart. Restore chooses the root/ancestor unit and its marked descendants, retaining independently archived branches. Captures distinguish live-parent and archived-unit states; separate canned activity captures are labelled fixtures.

## UI fixes and contracts

Onboarding hints previously survived their owning panel and covered Calendar/Tasks. The hint now checks its panel is still visible after delayed scheduling and observes panel close to dismiss. Chromium regression closes Calendar, opens Tasks and verifies no stale hint remains. Browser tests activate Brain, Skills, Workbench and Phalanx from keyboard, check focused controls and Escape closure at desktop/mobile. Attachment preview add/remove is checked at both widths; backend upload ownership/vision tests run separately.

The frontend is served without a bundler. Changed JS syntax and `git diff --check` are checked. `npm ci --prefer-offline` installed pinned dependencies; no dependency versions changed, so this branch does not introduce a dependency-reinstall requirement.

## Configuration and upgrade behavior

- `src/env_aliases.py` maps `AGAMEMNON_*` to legacy internal reads before application configuration. Canonical value wins when both exist; no secret values are logged. Stock CPU/NVIDIA/AMD Compose files now forward canonical counterparts for all their legacy variables. `.env.example` documents this.
- `APP_PUBLIC_URL` is the common OAuth origin, with integration-specific overrides retained. Both Google and MCP consult saved configuration; MCP builds redirect metadata dynamically. Existing already-registered OAuth clients may require reauthorization after origin changes.
- `GITHUB_HOST` derives REST host when no explicit API base exists. Invalid host fails closed. Private enterprise/live credential validation requires credentials and was not attempted.
- Internal calls emit `X-Agamemnon-Internal-Token`; server still accepts `X-Odysseus-Internal-Token` with the same loopback/token protections. Canonical owner/poll headers are accepted, and legacy aliases remain. Webhooks emit canonical and old event/signature headers for consumers upgrading separately.
- Existing Claude workflow run names are recognized alongside new Agamemnon names. Existing persisted reminder persona ID `odysseus` now displays and prompts as Agamemnon; no existing scheduled task is stranded.
- Remote repository URLs, artifact filenames, plugin globals, executable paths, browser storage keys and database integration IDs require explicit migration/aliases. They were not blindly rewritten. This is **remaining migration work**, not a blanket exemption from the requested rename. Only the CI deployment image was explicitly excluded by the user.

## Evidence and provenance

Untracked `.visual-check` holds scripts/logs. `capture.py` launches `python app.py` in isolated storage with sign-in on, bind restricted to loopback, MCP/pollers/tasks disabled. It deletes temporary data on shutdown. No operator data/config/loadouts are touched. It captures login, blank slate, Phalanx, notes, tasks, Calendar, Brain, Workbench and Settings at 1440/700/390, plus real unit archive states. Manifest records source SHA, time, width and fixture distinction.

Focused resume checks:

- 331 Python tests passed across archive, worker launch, send-to-session, OAuth/GitHub configuration and upload ownership.
- 113 middleware/webhook/foreground-gate tests passed.
- 8 Chromium login/chat/settings/font tests passed, 1 strict known xfail (nested Markdown fence).
- 5 new Chromium panels/Skills/keyboard/onboarding/attachment tests passed.
- New archive write-fault test injects SQLAlchemy flush failure and verifies flags, markers and cache unchanged; concurrent launch waits for archive and then refuses the archived parent.
- Canonical Compose forwarding, invalid GitHub URL rejection, saved Public URL provider metadata tested locally without external credentials.

The first resumed full lane failed: 58 failed, 11,030 passed, 4 skipped, 3 xfailed. Most failures were in-memory workflow tests exercising a missing persisted row; launch now rejects a durably archived row while leaving existing manager/ownership validation intact. The unit test for the new transaction boundary and the README guard were updated, and the bundled-skill history was regenerated so existing installs can upgrade unchanged skill bodies. The repaired workflow/database/skill group had 129 passes with one README markup expectation subsequently corrected. The final rerun is separate and must be read before accepting the commit.

Historical full lane on `4666ad9d` was 11,076 Python passed, 4 skipped, 3 xfailed; Node 520 passed, 2 TODO. It is **not** evidence for the resumed changes or final HEAD. The final resumed full-lane output/result is in `resume-full-final.log`/`resume-full-final.exit` and the handoff. Failed attempts and corrected test setup are retained; successful claims are based on actual outputs, not delegation claims.

## Blockers and remaining release work

1. Complete compatibility-aware rename: plugin/executable/browser-key aliases and remaining current product prose. Historical snapshots should be labelled as historical rather than falsified.
2. Docker executable/daemon is absent here; installed package is `chromadb-client`, not a Chroma server. Stock Compose image build/start and healthy vector storage are therefore unverified. No live vector endpoint or app environment was read to bypass isolation.
3. No live model/provider credentials were supplied. Scripted model proves protocol/stream/persistence/handoff contracts only. A useful first task with an actual model and healthy vector retrieval remains a release gate.
4. External mail/calendar/Todoist/Penpot/Windows Pi live integrations need fresh-account credentials and sandbox services. Local owner/security/configuration regressions are evidence, not substitute live credentials.
5. Repair pre-existing nested Markdown fence browser xfail. Formal contrast audit, every deep panel and native Windows/macOS launcher rendering are not complete.
6. Parent arranges the single independent read-only review of the final commit and capture evidence. No additional reviewer/writer or publication request is created by this worktree.

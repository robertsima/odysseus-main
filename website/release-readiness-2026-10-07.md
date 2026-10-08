# Release readiness review: 7 October 2026

## Decision and provenance

**Ready for independent review, not cleared for public release.** Local archive/configuration/UI defects have repairs and regressions. Stock-container startup with healthy vector retrieval, live provider/integration credentials and native packages remain release gates.

Repository: `/app/data/development/odysseus-main`. Worktree: `/app/data/agent_worktrees/release-readiness`. Branch: `agent/odysseus/release-readiness`. Base: `origin/dev`, `c50e6511d91ac14438f2aa27219f541cdb8258a2`. Source dirty work was preserved. No live data/config/loadouts were changed. No publish request, push, deploy or merge was made.

The final tested source commit is the commit containing this report. Exact SHA, final full-suite result, capture times, runtime startup log and diff are recorded under `.visual-check/<that SHA>/verification.txt` and `manifest.json`. This avoids a self-referential SHA in a tracked report. Earlier full results are not evidence for that final commit: `4666ad9d` had 11,076 Python passes; `b6761b18` had 11,089. The final lane runs after this report is committed. Read its `full.log` and `full.exit`, not the earlier counts.

## PR #64 browser CI follow-up

Browser job `113167494764` in run `37733421427` failed only the plugin-application loadout test, with 211 other tests passing. The fixture replaced the legacy `OdysseusPluginCatalog` global while the renamed editor reads `AgamemnonPluginCatalog`. The actual catalog retains its legacy alias. The first correction changed the fixture to use the canonical global.

The resumed repair also supports extensions exposing only the legacy global. The editor prefers `AgamemnonPluginCatalog` and falls back to `OdysseusPluginCatalog` when the canonical object is absent. Parameterized coverage deletes both globals before installing either fixture, exercises plugin application and verifies unsaved personality and tool settings survive. Without the fallback the legacy case reproduces `Cannot read properties of undefined (reading 'onApplied')`; with it all six editor tests pass. Follow-up full/browser logs and fresh captures are keyed to the repair commit. No earlier successful run is attributed to these changes.

The running harness image update did not integrate source. Fetch on 8 October still resolves `origin/dev` to approved exact target `4ac7d4c6c8817b785ddfb5638c797158de857920`. The managed merge retry refuses with `linked_worktree_read_only`; sandbox Git cannot access source metadata. No bypass was attempted. Integration and combined-code revalidation remain a separate required gate.

## Seven criterion outcomes

| Criterion | Outcome and observed checks |
|---|---|
| Phalanx accurately represents agents | PASS locally. Durable parent/child grouping, unit archive/restore and restart ancestry are covered. Real isolated backend captures show a parent plus five children: six live rows, zero live after archive, six archived, six restored. Pending approvals after a finished turn, failed writes, independently archived nested descendants and child-launch races have regressions. |
| Functionality/UI/reliability sweep | PASS for local tested scenarios, release-gated for external services. Fresh-process sign-in, streaming/persisted model reply, worker handback, major-panel keyboard/focus, attachments and owner/error paths are checked. The nested Markdown fence defect is repaired; its former strict browser xfail is now a passing assertion. Full regression evidence is keyed to the final commit. This is not an exhaustive accessibility or external-service certification. |
| Agamemnon theme and fonts | PASS on browser default/saved-choice scenarios. Agamemnon/Space Grotesk at 1440/700/390; login, composer, Brain/Skills, Workbench, Phalanx, Settings and selected-font persistence. Standalone OAuth pages now share hosted Space Grotesk, charcoal/gold palette and focus outlines. Native launcher rendering/build is unverified here. |
| Product rename | Canonical UI/prompt/setup/export branding and compatibility bridges implemented. Profile/plugin imports accept both versions of the format name; new downloads/setup use Agamemnon. Browser preference migration, plugin global alias, CLI dispatcher, client ZIP canonical/legacy paths, header aliases and portable executable alias are covered below. CI deployment image is unchanged. Historical audits, old inputs and internal identifiers remain explicitly documented, not exempted as current product branding. |
| Blank slate | PASS for isolated local startup and model protocol contracts; blocked for healthy-vector/live-model stock installation. Fresh temporary app storage has no personal sessions/notes/memory/profiles. Suggested skills are seeded by ordinary startup, not copied from personal data. A separate scripted model tests streaming, save/reload and worker return. It does not prove model quality or actual provider authorization. |
| Operator-specific coupling | Removed Pi drive/script/vault/hardware assumptions; operator settings replace them. Repository base follows discovery rather than assuming dev. Client guidance uses configured directories instead of named personal vaults. No personal notes/memory/saved agent skills are used as blank-slate evidence. Windows execution remains credential/environment-gated. |
| Environment complexity | Canonical forwarding in CPU/NVIDIA/AMD Compose; shared Public URL OAuth defaults; MCP resolves saved URL at provider creation; invalid GitHub host fails closed before credentials are routed. Environment conflicts favor canonical values; secret values are not logged. Contract tests cover forwarding, URL changes and invalid-host refusal. |

## Archive correctness

`core.database.archive_session_unit` persists flags and `archived_with` markers in one transaction. Flush failure rolls back every row and leaves cache flags unchanged. Missing/foreign rows or invalid settings fail closed. Independently archived descendants retain their own unit marker and are not restored with a later ancestor unit. **Persisted restore was already transactional** through `unarchive_sessions`; the earlier report claiming individual restore commits was wrong.

`src.agent_lifecycle.unit_lock` coordinates managed worker creation/registration, send-to-session child creation/busy registration, archive validation/commit and restore. Archive queries the real approval store for every target, including finished turns. Expired/retired approvals follow store semantics. No model/tool await is held in the critical section. This is a process lock: the supported deployment is one app process, not multiple Uvicorn workers.

All archive/restore entrypoints now use `src.agent_lifecycle.change_archive`: Phalanx, sidebar and bulk HTTP actions, and the session-management tool. Ownership, traversal, active work and pending approvals are checked under the same lock; flags and markers commit at one database boundary. Standalone chat HTTP response formats remain unchanged. Regression coverage includes sidebar/tool approval refusal, active children, unit restoration and rollback.

Tests: `tests/routes/agents_routes/test_archive_takes_the_unit.py`, `test_unit_archive_atomic.py`, `test_all_archive_entrypoints.py`, worker/send-to-session regressions. Focused lifecycle/session checks passed 201 tests. Captures distinguish real persisted unit ancestry from separately labelled canned activity fixtures.

The mandatory storage migration module is precached by service-worker cache version `agamemnon-v391-storage-migration`. `tests/static/sw/storage_offline.test.mjs` installs the actual worker into simulated CacheStorage, disables the network with no HTTP cache, fetches both storage modules through the worker and executes the module graph. Both service-worker tests pass. This is an executable offline contract, not a browser certification.

## UI and upgrade repairs

- First-use hints are dismissed with their owning panel, including delayed scheduling. Notes help belongs to the Notes stacking context and cannot cover Brain. Browser checks use `elementFromPoint` for the reproduced overlay.
- Markdown closing fences must be on their own line and at least as long as the opening fence. Nested shorter fences remain literal code. Unit tests cover inline backticks and longer closers; the real-process browser regression asserts one code block and marker placement.
- Keyboard activates Brain/Skills/Workbench/Phalanx at desktop/mobile, checks control focus and Escape closure. Attachment previews add/remove at both widths; upload ownership/vision is tested separately.
- [Upgrade contracts](brand-upgrade.md) enumerate canonical profile format, plugin bundle paths/global, client environment aliases, CLI dispatcher, Windows executable alias, local-storage migration, header compatibility, reminder personas and retained service/registry identities. Old imports and dummy credentials have local regressions. New client setup replaces either old/new marketplace entry rather than adding another configured product.
- Stored data, Git registries, branch names and service identities are not globally rewritten. They remain supported transition inputs so an upgrade cannot create a second scheduler or strand approvals. Historical source references are preserved for audit provenance.
- No dependency versions changed. Test dependencies were installed locally with `pip install -r requirements-test.txt`; frontend pinned packages were installed with `npm ci --prefer-offline`.

## Checked locally

Historical focused runs before this follow-up:

- 156 profile/plugin/Markdown/client authorization tests passed; 261 client/header/email/scheduler contracts passed; 626 Git/Claude/Cookbook tests passed; 626 Git/Claude/environment/MCP contracts passed (overlapping groups, not additive unique coverage).
- 13 Chromium live-chat/panels/font tests passed, including the former nested-fence xfail. Earlier browser failures were missing Playwright in a reset sandbox; installed test dependencies and the required-browser lane passed.
- 14 Node migration/chat/poll tests passed; 14 theme/migration tests passed (overlap).
- Shell script syntax, changed JS syntax and `git diff --check` checked.
- Final verification found two stale Node fixtures reading/writing the legacy toggle key after migration. The chat fixture and assertions now use the canonical key; separate storage-migration tests retain legacy-input coverage. The focused rerun passed 11 tests. The failed earlier full lane remains in its original evidence directory rather than being reported as green.

Final full-lane and capture provenance is in the final commit's evidence directory. Capture command: `python .visual-check/capture.py`. It starts `python app.py` with isolated temporary storage, new account, loopback binding, MCP/pollers/tasks disabled; no live settings or data are read. Login/empty panels use a real backend; active-agent activity is separately marked as canned. Standalone OAuth HTML is rendered locally without performing authorization. Captures add desktop, 700px and mobile states and real unit archive/restore counts. Screenshots do not establish live integration behavior.

## Reproducible release gates

This sandbox has Docker CLI 29.6.2, but no usable daemon socket and no Compose plugin (`docker compose ...` reports unsupported flags). Python has `chromadb-client`, not a server. Do not call those missing checks passes.

On an isolated Docker-capable test host, from this branch:

```sh
# Use a new folder, never the operator's live app/log directories.
export APP_DATA_DIR="$PWD/.release-sandbox/data"
export APP_LOGS_DIR="$PWD/.release-sandbox/logs"
export APP_BIND=127.0.0.1 APP_PORT=17000
mkdir -p "$APP_DATA_DIR" "$APP_LOGS_DIR"
docker compose -p agamemnon-release-check -f docker-compose.yml config --quiet
docker compose -p agamemnon-release-check -f docker-compose.yml build
docker compose -p agamemnon-release-check -f docker-compose.yml up -d
docker compose -p agamemnon-release-check -f docker-compose.yml ps
docker compose -p agamemnon-release-check -f docker-compose.yml logs --no-color chromadb odysseus
```

Require healthy ChromaDB before accepting startup. Create a fresh account at `http://127.0.0.1:17000`, configure only a sandbox model, send a useful task, verify its persisted reply after reload, add a test note, index it and verify retrieval returns that note. Record model/server versions and commands alongside captures. Do not reuse production API tokens. Stop only the named test project after the check; no production volumes are involved.

Other gates: fresh-account mail/calendar/Todoist/Penpot/Windows Pi credentials and sandbox services; native Windows/macOS package builds; formal contrast/accessibility checks; external plugin installation. Local tests establish owner/security/configuration contracts, not live authorization. The parent arranges exactly one read-only independent review of the final commit. No reviewer or new writer was started for this continuation.

Latest `dev` integration and revalidation are required before the final PR/merge. Robert has pushed additional changes. The parent must coordinate integration using confirmed exact heads; this continuation does not merge, rebase or pull those changes. Existing evidence covers only the named release-readiness commit, not the eventual integrated commit.

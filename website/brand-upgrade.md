# Agamemnon upgrade contracts

Agamemnon is the current product name and default visual theme. The CI deployment image name is unchanged. Existing installations must not lose their data or start a second scheduler during a name upgrade.

## Canonical interfaces and compatibility inputs

- Profile exports now use `agamemnon-agent-profiles` and canonical download filenames. Import and plugin-template loading accept both format names at version 1. No profile policy is loosened by importing an old document.
- Client ZIPs contain both `agamemnon/` and the previous installation tree. New Settings instructions register `agamemnon@personal`, replacing either old or new marketplace entries. Old helper paths and scoped tokens keep working. Canonical helpers accept `AGAMEMNON_URL` and `AGAMEMNON_API_TOKEN` first, then the old variables. Scoped `/api/codex/*` endpoints are unchanged.
- `python scripts/agamemnon <command>` is the canonical CLI dispatcher. The executable `scripts/odysseus-*` implementations remain command aliases. Both use the same data and API paths, not separate stores. CLI help uses Agamemnon.
- New portable Windows builds produce `Agamemnon.exe` with `Odysseus.exe` as a shortcut-compatible copy in the same directory. `Odysseus.spec` is a build-file alias whose output is canonical. Existing portable users should retain their data directory while replacing program files; this Linux environment did not execute a Windows build.
- Local-storage preference access maps the branded prefix to `agamemnon`. Legacy values migrate lazily on read, canonical values win conflicts, writes mirror to legacy keys for older tabs/extensions, and removal clears both. Unbranded origin keys and session storage are untouched. Remaining old preference literals are alias callers, not independent stores.
- `window.AgamemnonPluginCatalog` is canonical. `window.OdysseusPluginCatalog` points to the same object for installed extensions.
- Run identity, passive polls, internal tool attribution, webhooks and generated email have canonical headers. Run/email replies emit legacy headers as well; readers accept both where needed. Old reminders, approval records and unit ancestry do not need a destructive data rewrite.
- The reminder persona `agamemnon` aliases the same definition as `odysseus`, so persisted schedules keep their voice. Old reminder subject prefixes remain recognized to prevent scanning the app's own mail as urgent.
- Python startup resolves canonical `AGAMEMNON_*` environment values before consumers read the legacy variables. Compose forwards both. Conflicting canonical values win. Existing data directories, registries, Git branch namespaces and systemd/Compose service identities remain shared compatibility identities, not a new second application. Do not install a second service beside an existing scheduler.
- Historical audits, reproduced failures, repository owner/slug URLs, tool symbol names and existing database/runtime identifiers remain source-traceable. They are not current UI branding or an instruction to create personal content. Technical symbol renaming alone would break plugins without improving setup.

## Provenance and limitations

Regression sources: `tests/src/test_brand_upgrade.py`, `tests/src/tool_capabilities/test_agent_profile_transfer.py`, `tests/static/js/storageBrandMigration.test.mjs`, `tests/src/env_aliases`, and archive/configuration tests named in the release report. Canonical and legacy imports are exercised; credential tests use only dummy values.

The old Git/Compose/systemd identities are transitional aliases. Removing them requires a separately tested migration of installed services and managed registries, not a global text replacement. Native package execution, old-tab/new-tab simultaneous storage conflicts and a full external Codex/Claude plugin installation remain release gates beyond the local contract tests.

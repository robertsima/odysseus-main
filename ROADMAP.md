# Roadmap / Help Wanted

Odysseus is on a voyage, but not home yet. It works great for me (lol), but this ship is moving fast and feedback/help would be appreciated! (I don't know what I'm doing, help).

If you see weird CSS, strange layout behavior, or a suspiciously murky corner of
the codebase, you are probably right to stay away.

## High Priority

- [x] Add scoped Git inspection, staging/commits, branches/tags, switching,
  fetch/pull/upstream configuration and confirmation-bound push/merge/deletion
  without a private-vault grant; preserve unresolved URL follow-ups.
  See [local repository sync](website/agent-worktree.md#updating-an-existing-checkout-scoped-git).
- [x] Fix native Git calls rejected for unused empty parameters; preserve optional
  fields through Responses conversion, action guidance through schema compaction,
  exact single-use confirmations, and routing for pasted Git credential errors.
- [ ] After rebuild, verify local Git sync against the deployed Umni checkout:
  exact configured upstream, clean fast-forward, dirty/missing-branch refusal,
  and revoked GitHub-read/tool permissions. Do not assume the checkout directory
  name is the GitHub repository name.
- [x] Add bounded `pull_with_restore` for dirty checkouts, preserving staged,
  unstaged and untracked paths without exposing arbitrary stash-pop behavior.
- [x] Extend typed Git coverage to clone/init, bounded stash management,
  recovery-ref reset/rebase, force-with-lease and lease-bound remote branch
  deletion. Risky actions use exact single-use confirmations; arbitrary force,
  interactive rebase and conflict-resolving merge remain intentionally unavailable.
- [x] Repair corrective follow-up routing, distinguish MCP usage from MCP
  administration, defer irrelevant connected tools, and align advertised tools
  with private-vault execution permission. See
  [September 17 log follow-up](website/harness-log-review-2026-09-17.md).
- [ ] Design genuinely isolated general repository execution so builds/tests can run
  without granting access to the mounted private vault. A working directory
  or prompt restriction is not an isolation boundary.
- [ ] Measure cross-turn cache reuse and large-tool-output growth in production;
  evaluate pre-turn compaction and bounded output offloading with task-success
  checks before changing the execution ledger.

- [x] Per-agent persisted personality/instructions, declarative capability plugins,
  pinned PromptScript/skills CLIs, and reviewed draft-only portable skill imports.
  See [agent extensions guide](website/agent-extensions.md).
- [ ] Verify agent extension controls after deployment: two contrasting personas,
  plugin enable/remove with manual permission edits, local skill import/publish,
  and native/Docker CLI readiness.

- [x] Separate shared human-intent assessment, advisory candidate selection,
  and execution authorization; add bounded per-turn capability discovery for
  native and fenced/MCP models. See
  [harness routing design](specs/harness-capability-routing.md).
- [ ] Run the native/local-provider [routing acceptance matrix](website/harness-routing-acceptance.md)
  after deployment; compare task success, total tokens, cached input and
  latency, including eight concurrent agents and revoked permissions.

- SQUASH BUGS
- Fresh install smoke tests on Linux, macOS, and Windows. Docker, native Python,
  and WSL all need coverage.

- Integration audit: do integrations even work? Confirm what works, what needs setup docs, and what should be removed or hidden. 
- Cookbook reliability on other computers. This is probably the area most likely to need work across different machines, GPUs, drivers, shells, and Python environments.
- Cookbook SGLang support across platforms. Make sure SGLang setup/serve works
  predictably on Linux, Windows/WSL, macOS where possible, Docker, and common
  NVIDIA/AMD hardware paths.
- Deep Research model presets by hardware. Recommend approved model/parameter
  profiles for small, medium, and large local setups so people with different
  hardware can use Deep Research without guessing. Surface this either in Deep
  Research settings or as a Cookbook scan/dropdown suggestion.
- Cookbook model scan/download ranking. Prioritize newer architectures and
  better hardware-fit models instead of scoring everything almost the same.
  Ranking should account for architecture age, quant format, VRAM/RAM fit,
  backend support, vision/mmproj requirements, and likely serve reliability.
- Cookbook error feedback and logging. Failed downloads, dependency installs,
  preflights, and serve jobs should show the actual command/output/error in the
  UI, with copyable logs and clear next steps instead of just "crashed".
- Agent prompt/context bloat. Agent mode is too heavy for smaller local models:
  tool schemas, skills, memory, documents, and instructions can eat the context
  before the user request really starts. We need slimmer prompts, better tool
  selection, smaller default tool sets, and clearer guidance for models with
  4k/8k/16k context windows.
- Local model speculative decoding support. For Odysseus-tuned local models,
  plan to ship or recommend a small same-tokenizer draft model when the serving
  backend supports it. Early vLLM testing showed a generic `Qwen3-0.6B` draft
  beside `Qwen3-8B` can materially reduce wall time, while an unsupported
  DSpark conversion performed poorly. Treat this as a supported draft-model lane
  first; keep MTP-specific packaging as future work only when the architecture
  and runtime support are real. Judge this by time-to-success, tool correctness,
  grammar, and unchanged target output, not tokens/sec alone.
- Skill/tool prompt-injection audit. User-editable skills, notes, documents,
  fetched pages, and memories should be treated as untrusted data. Keep testing
  whether models follow malicious instructions from those surfaces.
- Better degraded-state reporting for ChromaDB, SearXNG, email, ntfy, and provider probes.
- Email performance audit. Fetching, searching, opening, deleting, and sending
  email can feel slow, especially over IMAP/SMTP providers with high latency.
  Need someone who knows mail performance to profile the current flow, identify
  whether the bottleneck is IMAP folder select/fetch, cache invalidation,
  attachment/body loading, SMTP handshakes, or frontend refresh behavior, then
  propose safer caching/prefetch/batching without breaking multi-account state.
- Provider setup/probing audit for Anthropic, Gemini, Groq, xAI, OpenRouter, OpenAI, and DeepSeek.

## Redistributable Control Plane

The first productization pass is now in place: schema-driven configuration,
capability detection, provider-neutral delegation, Markdown notes in the vault,
folder/file access policy, a human vault explorer, and enforceable per-agent
loadouts. The living behavior specification is
[`specs/personal-directories-and-tool-routing.md`](specs/personal-directories-and-tool-routing.md).

### Next

- [ ] Add versioned agent loadout presets: clone, rename, import/export, diff,
  restore defaults, and preview the effective policy before save. Agent-side
  authoring already exists (`manage_agent_loadout`); this is the human UI.
- [ ] Add temporary per-run grants with expiry and revocation for private vault
  reads, write tools, shell/host control, integrations, and model switching.
- [ ] Add an admin-readable policy audit log covering profile changes, grants,
  denied tool calls, private reads, agent messages, and approval decisions.
- [ ] Add a policy inspector explaining why a selected agent can or cannot use
  a tool, model, MCP server, memory operation, or vault path.
- [ ] Turn the monitoring view into a live task topology: parent/child/peer
  links, active objective, critical path, waiting/approval state, last message,
  and failure propagation without opening every chat.
- [ ] Add per-agent budgets for turns, wall time, context, tool calls, parallel
  children, and optional provider spend. Surface approaching limits before a
  task is interrupted.
- [ ] Add loadout assignment rules for scheduled tasks, subagents, externally
  triggered jobs, and named agent roles—not only already-running sessions.
- [ ] Complete vault file management: create folder/note, move, rename, delete
  with recovery, drag-and-drop, keyboard navigation, favorites, and recently
  opened files.
- [ ] Add vault indexing telemetry: mounted-root health, discovered/indexed/
  skipped counts, current file, stale index warning, reindex progress, and
  actionable Chroma/embedding errors.
- [ ] Add a policy-aware search preview so an administrator can compare public
  results with results available to a selected private-enabled agent without
  exposing private excerpts to unauthorized sessions.
- [ ] Handle external-edit conflicts explicitly with file revision checks,
  reload/compare/overwrite choices, and autosave recovery.
- [ ] Add first-class directory pickers, validated endpoint/host controls,
  connection-test buttons, secret replacement flows, and visible restart
  requirements throughout Configuration.
- [ ] Continue replacing open text fields with selects only where the value is
  truly finite. Retain validated custom entry for provider model names, URLs,
  paths, prompts, secrets, and extensible integration identifiers.
- [ ] Add role templates for administrator, standard human, trusted local
  agent, hosted model, research agent, and untrusted external integration.
- [ ] Verify owner scoping and policy inheritance under real multi-user use;
  add cross-owner denial tests for vault reads, profiles, presets, activity,
  messages, and external-agent tokens.
- [ ] Accessibility and small-screen pass for the Agent Control Room, vault
  tree, settings tabs, dropdowns, resizing, focus order, reduced motion, and
  screen-reader status announcements.

### Completed in the current pass

- [x] Agent-authored worker loadouts: an agent can create, update, delete and
  start a loadout, clamped to its own chat's policy with every narrowing
  reported back (`manage_agent_loadout`, `src/agent_loadouts.py`).
- [x] Theme, custom themes and navigation order follow the signed-in account
  across browsers, reconciled newest-wins against `/api/prefs` on every boot
  (`static/js/serverPrefs.js`).
- [x] Capability registry, availability checks, requirement recheck, and
  settings-schema metadata.
- [x] Collapsed-by-default capability panel and functional advanced-settings
  navigation.
- [x] Dropdowns/live inventories for finite endpoint, model, speech-provider,
  and similar configuration values while preserving valid custom values.
- [x] Markdown note migration and unified vault indexing.
- [x] Folder-wide `public`, `private`, and `readonly` model policy with per-file
  sensitivity overrides and fail-closed validation.
- [x] Human vault explorer/editor with collapsed folders, readable hierarchy,
  search, policy badges, and clear human-versus-model access language.
- [x] Provider-neutral delegation, peer-agent messaging, capability-aware tool
  exposure, and subscription-provider groundwork.
- [x] Integrated Agent Control Room with concurrent monitoring, dock/expand
  behavior, resizable fleet view, and persistent navigation ordering.
- [x] Per-agent presets and enforceable controls for tools, skills, memory,
  models, MCP/integrations, delegation, parallelism, approvals, and private
  vault access.

## Refactor Targets
- CSS cleanup. `static/style.css` basically Calypso's island atm.
- Tour core helper. The onboarding tours have too much copy-pasted scaffolding; promote a shared `tour-core.js` helper before adding more tours.
- Modal/window positioning cleanup. Some window controls have improved, but the
  underlying popup/dropdown/fixed-position behavior is still too fragile.
- Mobile media override discoverability. A lot of "CSS did not move" bugs are mobile `@media` overrides of the same selector; comments or linting around desktop/mobile paired rules would help.
- Dead code pass for old routes, stale feature flags, and unused UI states.

## Frontend

- Expand the Editor for quicker, more robust everyday use. Better file/document
  handling, smoother window behavior, clearer save/export flows, stronger image
  editing affordances, and fewer brittle edge cases.
- Better AI integration for Notes and Todos. Notes should be easier for the
  agent to read, update, summarize, and turn into actions. Todos should be
  assignable to an agent from the UI, possibly through a button, task action,
  or dedicated skill/tool flow.
- Mobile gallery/editor polish. Easier to launch/download inpaint model or any missing pieces.
- Accessibility pass: keyboard navigation, focus states, contrast, reduced motion.
- Improve empty states and error messages on fresh installs.
- Tighten first-run setup, hints, and tours so they do not repeat or fight each other.
- Vendor CDN assets eventually for a more fully self-hosted/offline mode.

## Backend

- More tests around endpoint probing and provider setup.
- Better task scheduler defaults and visibility.
- Close the database-exhaustion deployment follow-up: without enlarging the
  pool, validate simultaneous background jobs plus model refresh while ordinary
  database endpoints remain responsive; verify cancellation leaves no stuck
  task-run rows, subscription auth refresh stays owner-scoped, and deliberate
  exhaustion produces safe `503` responses with useful occupancy diagnostics.
  The 2026-09-17 incident and acceptance checklist are tracked in
  `specs/harness-reliability-efficiency.md`; the fix is not yet deployed.
- Backup/restore guide and helper flow for `data/`.
- Security hardening around admin-only tools and clear docs for their risk.

## Not The Focus Right Now

I prob shouldnt add more themes.

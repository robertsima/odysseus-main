# Configuration: settings vs. environment

Odysseus grew as a personal deployment on one NAS before this split existed, so
two configuration systems drifted apart: `data/settings.json` (94 keys, most
with a UI control — see `src/settings.py` and `src/settings_schema.py`) and
123 `ODYSSEUS_*` environment variables, only a handful of which have any UI at
all. This document inventories every `ODYSSEUS_*` variable actually read by
the code and says where each one belongs, so the migration is traceable and a
future contributor knows where to put the next option.

## The rule

**`settings.json` holds CHOICES.** Anything a person might reasonably want
different on any host — a preference, a model, a folder name, a cadence, a
limit — belongs there, with a UI control. Two installs of the identical
software, on the identical machine, with different owners, can legitimately
want different values.

**The environment holds PLACEMENT.** Anything that changes because *this copy
of the software happens to run here* — mount paths, ports, service URLs,
secrets, and feature opt-ins that are properties of the deployment rather than
the user — belongs in the environment. The test is not "is this a path" or
"is this a boolean"; it's "would the same person, running the same install on
a different box, plausibly want this different only because the box is
different?" If yes, it's placement. If they'd want it different because *they*
are different — different taste, different hardware budget for a cache,
different tolerance for a limit — it's a choice.

A variable can be both, in sequence: `src/settings_schema.py` already declares
`vault_directory` as a UI setting whose value is *locked* when
`ODYSSEUS_PERSONAL_DIR` is set, because a container's bind mount fixes that
path at compose time and letting someone type a different path into the UI
would silently point the app at a directory that doesn't exist inside the
container. That pattern — a real setting, overridable/lockable by an
environment variable for deployments where the value is fixed by the compose
file — is the template for anything that is a choice in general but placement
in a specific topology (see `src/agent_worktree/config.py` and
`src/personal_dirs_config.py` for two more instances of the same idea).

Security/trust escape hatches (`ODYSSEUS_ENABLE_HOST_DOCKER`,
`ODYSSEUS_ALLOW_PRIVATE_CALDAV`, `ODYSSEUS_MCP_ALLOWED_COMMANDS`, ...) are
treated as PLACEMENT throughout this document even though they read like
booleans a user "chooses." The reasoning: the correct value is a property of
*where and how trusted* this deployment is (a personal box on your own LAN vs.
a shared/hosted instance), not a personal preference about behavior — and
putting a widened attack surface behind a UI toggle in `settings.json` means
anyone who can edit settings (not just whoever provisioned the box) can widen
it. Environment variables require host/container access to set, which is the
right bar for these.

## Method

Every variable below was found with:

```bash
grep -rhoE "ODYSSEUS_[A-Z0-9_]+" --include=*.py src/ routes/ services/ core/ app.py scripts/ integrations/ | sort -u
```

123 distinct tokens matched. Each was traced to its actual read site(s) before
being classified — several names that look like configuration are not:
they're local variable names inside bash scripts that Cookbook generates and
ships to a model-serving host, or fragments of output-parsing markers, that
happen to start with `ODYSSEUS_` too. Those are called out separately rather
than forced into PLACEMENT/CHOICE/DEAD, because giving them a verdict at all
would misrepresent them as configuration.

**Totals: 123 tokens — 43 PLACEMENT, 37 CHOICE, 5 ambiguous (flagged, not
forced), 38 not real configuration variables (grep noise).**

**Scope caveat:** the grep above (per the task brief) does not cover
`mcp_servers/`, where the built-in MCP server implementations live.
`.env.example` already documents `ODYSSEUS_PI_WORKER_SCRIPT`,
`ODYSSEUS_PI_WORKER_ROOT`, `ODYSSEUS_PI_WORKER_IDENTITY_FILE`, and
`ODYSSEUS_PI_DOCUMENTATION_ROOT` (read by `mcp_servers/pi_worker_server.py`,
not by anything in the grepped directories), which are all PLACEMENT by the
same reasoning as `ODYSSEUS_PI_WORKER_HOST`. A follow-up pass should re-run
the inventory over `mcp_servers/` to confirm there's nothing else there.

`CLEANUP_INTERVAL_HOURS` is a known non-prefixed legacy environment variable
that controls a cadence and therefore belongs with CHOICES. It is deliberately
not shown in `.env.example`; the settings migration should replace its current
environment-only read before adding any new deployment documentation for it.

---

## PLACEMENT — stays in the environment (45)

| Variable | Read at | What it controls | Verdict |
|---|---|---|---|
| `ODYSSEUS_DATA_DIR` | `src/constants.py:12` | Root of the whole `data/` tree (settings, sessions, db, auth, cache, uploads). The single source of truth for every other path constant. | PLACEMENT |
| `ODYSSEUS_PERSONAL_DIR` | `src/settings_schema.py:169` (`env_override`) | Root of the Markdown vault. Exposed as the `vault_directory` UI setting, but *locked* to this value when the container's bind mount fixes it — see "The rule" above. | PLACEMENT |
| `ODYSSEUS_PERSONAL_DIRS` | `src/personal_dirs_config.py:44` | Declares `path:label` vault trees to auto-register/label at boot, so a compose file's bind mounts are enough to reproduce a working install without a manual `POST /api/personal/add_directory`. | PLACEMENT |
| `ODYSSEUS_MAIL_ATTACHMENTS_DIR` | `src/constants.py:59` | Where inbound mail attachments are stored on disk. Defaults under `DATA_DIR`; overridden to move it to a different mount. | PLACEMENT |
| `ODYSSEUS_AGENT_STATE_DIR` | `src/agent_worktree/config.py:114` | Where agent-worktree approval state lives on disk. | PLACEMENT |
| `ODYSSEUS_AGENT_WORKTREE_ROOT` | `src/agent_worktree/config.py:109` | Where the agent's git worktrees are created. | PLACEMENT |
| `ODYSSEUS_AGENT_SOURCE_REPO` | `src/agent_worktree/config.py:99` | Path to the git checkout the agent worktrees branch from. Required in a container — the image has no `.git`, so the default (app root) isn't a checkout. | PLACEMENT |
| `ODYSSEUS_AGENT_REPO` | `src/agent_worktree/config.py:95` | The one `owner/name` GitHub repo this deployment is allowed to publish agent branches to. Deliberately not user-choosable per the worktree publish design. | PLACEMENT |
| `ODYSSEUS_AGENT_GITHUB_TOKEN` | `src/agent_worktree/config.py:121` | Fallback PAT for agent-worktree publishing when no GitHub App is configured. Secret. | PLACEMENT |
| `ODYSSEUS_AGENT_PUBLISH_ENABLED` | `src/agent_worktree/config.py:124` | Master switch for the agent's ability to push/open PRs at all. Off by default; a deployment-trust decision, not a preference (`src/capabilities_builtin.py` "worktree_publish"). | PLACEMENT |
| `ODYSSEUS_AGENT_ALLOW_BASH_CLAUDE` | `src/agent_tools/claude_code_guard.py:24` | Escape hatch letting an operator with their own setup disable the guard that redirects bare `claude` shell invocations to the delegation path. | PLACEMENT |
| `ODYSSEUS_AGENT_ALLOW_BASH_PUSH` | `src/agent_worktree/push_guard.py:62` | Escape hatch for operators with their own git-push credential wiring in the container, disabling the guard that redirects `git push`/`gh pr create` to `manage_agent_worktree`. | PLACEMENT |
| `ODYSSEUS_GITHUB_API_BASE` | `src/agent_worktree/config.py:131` | GitHub API base URL for agent-worktree publishing; override for GitHub Enterprise. Service URL. | PLACEMENT |
| `ODYSSEUS_GITHUB_APP_ID` | `src/agent_worktree/config.py:132` | Numeric GitHub App ID for agent-worktree publishing credentials. | PLACEMENT |
| `ODYSSEUS_GITHUB_APP_INSTALLATION_ID` | `src/agent_worktree/config.py:133` | GitHub App installation ID. | PLACEMENT |
| `ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH` | `src/agent_worktree/config.py:134` | Path to the GitHub App's private key file (must be inside a mounted volume in a container). Secret material. | PLACEMENT |
| `ODYSSEUS_GITHUB_MCP_BINARY` | `src/builtin_mcp.py:156` | Path to the `github-mcp-server` binary, for native installs where it isn't on `PATH`/`/usr/local/bin`. | PLACEMENT |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | `src/github_credentials.py` | GitHub PAT shared by the permission-checked GitHub MCP and typed repository transport. Obvious non-GitHub tokens are rejected on GitHub.com. Secret. | PLACEMENT |
| `GITHUB_HOST` | `src/github_credentials.py` | Optional GitHub Enterprise host. Leave unset for GitHub.com; typed repository transport is currently GitHub.com-only. | PLACEMENT |
| `ODYSSEUS_GITHUB_MCP_WRITE` | `src/builtin_mcp.py:146` | Opt-in for the write-scoped GitHub MCP server and typed Git publish/remote-delete transport. Off by default and still bounded by each agent's `github_write` permission. | PLACEMENT |
| `ODYSSEUS_API_TOKEN` | `src/agent_tools/claude_code_tools.py:385` | Auth token Odysseus mints and hands to a delegated Claude Code / Codex child process so it can call back into this Odysseus instance's API. Secret. | PLACEMENT |
| `ODYSSEUS_URL` | `src/agent_tools/claude_code_tools.py:373` | Base URL of the running Odysseus instance, passed to the same delegated child process so it knows where to call back. | PLACEMENT |
| `ODYSSEUS_INTERNAL_BASE` | `src/constants.py:126` | Explicit override for the internal base URL Odysseus uses to call its own API (e.g. behind a TLS-terminating proxy). | PLACEMENT |
| `ODYSSEUS_INTERNAL_TOKEN` | `core/middleware.py:16` | Internal-only auth token for server-to-self calls; falls back to a random one generated at startup. Secret. | PLACEMENT |
| `ODYSSEUS_TOOL_EXTRA_ROOTS` | `src/tool_execution.py:211` | Extra filesystem roots the agent's file tools may touch, beyond `DATA_DIR` and temp. Explicitly documented as "declared by the DEPLOYMENT rather than post-boot admin state" so a bind-mounted workspace arrives already permitted. | PLACEMENT |
| `ODYSSEUS_MCP_ALLOWED_COMMANDS` | `src/agent_tools/admin_tools.py:140` | Comma-separated allowlist of extra shell commands permitted as stdio MCP servers. Empty by default; a per-host trust decision. | PLACEMENT |
| `ODYSSEUS_ENABLE_HOST_DOCKER` | `src/host_docker_access.py:8` | Opt-in for mounting/using the host Docker socket from inside the container. High-trust, deployment-level (given in the task brief as the reference PLACEMENT example). | PLACEMENT |
| `ODYSSEUS_DISABLE_MCP` | `src/builtin_mcp.py:235` | Global kill switch for all built-in MCP servers, for environments where the MCP subprocess model doesn't work. | PLACEMENT |
| `ODYSSEUS_BROWSER_EXECUTABLE` | `src/builtin_mcp.py:260` | Path to a browser binary for the built-in Playwright MCP server, when auto-detection (`google-chrome`/`chromium` on `PATH` or conventional locations) fails. | PLACEMENT |
| `ODYSSEUS_BROWSER_NO_SANDBOX` | `src/builtin_mcp.py:288` | Whether the built-in browser MCP launches Chromium with `--no-sandbox`. Needed in many containers/root environments; a property of this host's container runtime, not taste. | PLACEMENT |
| `ODYSSEUS_BROWSER_MCP_CACHE` | `src/builtin_mcp.py:414` | Directory for the Playwright MCP's npm/browser cache. Defaults under `DATA_DIR`; override to relocate it to another mounted volume. | PLACEMENT |
| `ODYSSEUS_BROWSER_MCP_REQUIRE_CACHE` | `src/builtin_mcp.py:236` | Locked-down/offline-install opt-in that refuses to `npx install` the browser MCP package at startup unless it's already cached — a network-policy property of the deployment. | PLACEMENT |
| `ODYSSEUS_PI_WORKER_HOST` | `src/builtin_mcp.py:84` | SSH host for the optional Windows "Pi worker" built-in MCP tool. A remote-service address. | PLACEMENT |
| `ODYSSEUS_SCRIPT_HOST` | `src/builtin_actions.py:417` | Default host the `run_script` builtin action targets when the caller doesn't name one (`localhost` or an SSH alias). Network topology. | PLACEMENT |
| `ODYSSEUS_FALLBACK_OWNER` | `routes/calendar_routes.py:64` | Fallback identity used only in single-user mode for requests that didn't resolve to an authenticated user. Comment explicitly frames it as a deploy-time override. | PLACEMENT |
| `ODYSSEUS_SINGLE_USER` | `routes/calendar_routes.py:65` | Whether this deployment runs single-user (no auth gate on identity resolution) or multi-user. Deployment topology, not preference — flipping it changes a security boundary. | PLACEMENT |
| `ODYSSEUS_ALLOW_PRIVATE_CALDAV` | `src/caldav_sync.py:59` | SSRF guard override letting CalDAV connect to private/internal IPs (e.g. a NAS-hosted CalDAV server on the same LAN). Right value depends on network trust, which is a deployment property. | PLACEMENT |
| `ODYSSEUS_ALLOW_OLLAMA_CLI_SCAN` | `routes/cookbook_helpers.py:544` | Windows-only opt-in for scanning installed Ollama models via the `ollama` CLI (skipped by default on Windows due to past blocking-call issues). OS/host-specific behavior. | PLACEMENT |
| `ODYSSEUS_INPROCESS_POLLERS` | `routes/email_pollers.py:1550` | Whether email pollers run in-process, or are disabled because an external cron/systemd driver (`scripts/odysseus-mail poll-scheduled`) fires them instead. Deployment architecture (single process vs. split workers). | PLACEMENT |
| `ODYSSEUS_INPROCESS_TASKS` | `app.py:1212` | Same as above for the scheduled-task runner: in-process vs. externally driven. | PLACEMENT |
| `ODYSSEUS_COPILOT_CLIENT_ID` | `src/copilot.py:35` | GitHub OAuth client id used for Copilot's device flow. Defaults to the public VS Code client id; override only if you register your own allow-listed GitHub App for a fork. | PLACEMENT |
| `ODYSSEUS_COPILOT_API_VERSION` | `src/copilot.py:40` | Dated API version header the Copilot API requires. A protocol-compatibility escape hatch, not a user setting — bumped only if GitHub changes the contract. | PLACEMENT |
| `ODYSSEUS_COPILOT_USER_AGENT` | `src/copilot.py:49` | User-Agent string sent to the Copilot API. Same rationale as API_VERSION. | PLACEMENT |
| `ODYSSEUS_COPILOT_INTEGRATION_ID` | `src/copilot.py:52` | Copilot integration id header. Same rationale. | PLACEMENT |
| `ODYSSEUS_COPILOT_EDITOR_VERSION` | `src/copilot.py:55` | Copilot editor-version header. Same rationale. | PLACEMENT |

## CHOICE — `settings.json` (36 migrated, 1 capability-specific fallback)

This table records the migration mapping. All entries now use the existing flat
`snake_case` convention in `src/settings.py`/`src/settings_schema.py` (for
example `stt_model` and `vault_directory`) with their old environment names as
one-way compatibility fallbacks, except `ODYSSEUS_MLX_IMAGE_VLM_MODEL`. That
last value belongs to the standalone Cookbook MLX server and remains a CLI/env
fallback until that capability has its own settings surface.

| Variable | Read at | What it controls | Proposed setting key |
|---|---|---|---|
| `ODYSSEUS_AGENT_APPROVAL_TTL_SECONDS` | `src/agent_worktree/config.py:48` | How long an agent-publish approval code stays valid (clamped 60–3600s, default 900). A limit, not a topology fact. | `agent_approval_ttl_seconds` |
| `ODYSSEUS_AGENT_BASE_BRANCH` | `src/agent_worktree/config.py:119` | Branch the agent's draft PRs target and diff against (default `dev`). A repo-workflow convention, not a deployment fact. | `agent_base_branch` |
| `ODYSSEUS_BROWSER_ISOLATED` | `src/builtin_mcp.py:285` | Whether the built-in browser MCP uses an ephemeral (`--isolated`) profile vs. a persistent one. A privacy/persistence preference. | `browser_isolated` |
| `ODYSSEUS_CHAT_UPLOAD_MAX_BYTES` | `src/upload_limits.py:8` | Chat/agent attachment upload size cap (default 10 MB). | `chat_upload_max_bytes` |
| `ODYSSEUS_EMAIL_COMPOSE_UPLOAD_MAX_BYTES` | `src/upload_limits.py:54` | Email-compose attachment size cap (default 25 MB). | `email_compose_upload_max_bytes` |
| `ODYSSEUS_GALLERY_UPLOAD_MAX_BYTES` | `src/upload_limits.py:42` | Gallery image upload size cap (default 100 MB). | `gallery_upload_max_bytes` |
| `ODYSSEUS_GALLERY_TRANSFORM_UPLOAD_MAX_BYTES` | `src/upload_limits.py:45` | Gallery transform input size cap (default 25 MB). | `gallery_transform_upload_max_bytes` |
| `ODYSSEUS_ICS_MAX_BYTES` | `src/upload_limits.py:60` | Calendar `.ics` import size cap (default 10 MB). | `ics_import_max_bytes` |
| `ODYSSEUS_MEMORY_IMPORT_MAX_BYTES` | `src/upload_limits.py:48` | Memory-import file size cap (default 10 MB). | `memory_import_max_bytes` |
| `ODYSSEUS_PERSONAL_UPLOAD_MAX_BYTES` | `src/upload_limits.py:51` | Personal-document upload size cap (default 25 MB). | `personal_upload_max_bytes` |
| `ODYSSEUS_STT_MAX_AUDIO_BYTES` | `src/upload_limits.py:57` | Speech-to-text audio upload size cap (default 25 MB). | `stt_max_audio_bytes` |
| `ODYSSEUS_IMAP_TIMEOUT_SECONDS` | `routes/email_helpers.py:1157` | IMAP connection timeout. A tolerance/limit preference. | `imap_timeout_seconds` |
| `ODYSSEUS_SLOW_REQUEST_LOG_SECONDS` | `app.py:230` | Threshold above which a request is logged as slow (default 0.75s). A diagnostics-sensitivity preference. | `slow_request_log_seconds` |
| `ODYSSEUS_STARTUP_WARMUPS` | `app.py:1089` | Opt-in to warm the tool index and model endpoints at startup, trading boot time for a warmer first request. A preference. | `startup_warmups_enabled` |
| `ODYSSEUS_MODEL_KEEPALIVE` | `app.py:1127` | Whether local models are kept warm between requests. A performance/resource preference. | `model_keepalive_enabled` |
| `ODYSSEUS_MISTRAL_REASONING_EFFORT` | `src/llm_core.py:1696` | Default reasoning effort for Mistral calls (default `high`). A model-behavior preference — the rule's own "a model" example. | `mistral_reasoning_effort` |
| `ODYSSEUS_RAG_FOCUSED_CAP_MULTIPLIER` | `src/rag_ranking.py:151` | How far the per-file chunk cap relaxes when a query names a tag/document. Retrieval-tuning preference. | `rag_focused_cap_multiplier` |
| `ODYSSEUS_RAG_LINK_EXPANSION` | `src/rag_ranking.py:167` | Whether `[[wikilink]]`-neighbor notes are pulled into retrieval. Retrieval-tuning preference. | `rag_link_expansion` |
| `ODYSSEUS_RAG_MAX_CHUNKS_PER_DOC` | `src/rag_ranking.py:137` | Per-file cap on chunks contributed to one retrieval (0 disables). Retrieval-tuning preference. | `rag_max_chunks_per_doc` |
| `ODYSSEUS_RAG_RECENCY_HALFLIFE_DAYS` | `src/rag_ranking.py:113` | How fast a note's rank decays with age. Retrieval-tuning preference. | `rag_recency_halflife_days` |
| `ODYSSEUS_RAG_TAG_CREDIT` | `src/rag_ranking.py:163` | Scales tag/alias match credit in ranking (0 disables). Retrieval-tuning preference. | `rag_tag_credit` |
| `ODYSSEUS_RAG_TEMPORAL_INTENT_WEIGHT` | `src/rag_ranking.py:119` | How much recency counts when a query is explicitly about "now"/"current". Retrieval-tuning preference. | `rag_temporal_intent_weight` |
| `ODYSSEUS_RAG_TEMPORAL_WEIGHT` | `src/rag_ranking.py:121` | How much recency counts as a tie-break on an ordinary query. Retrieval-tuning preference. | `rag_temporal_weight` |
| `ODYSSEUS_SAM_MODEL` | `routes/gallery/gallery_routes.py:59` | Segmentation model id for the gallery's SAM tool (default `facebook/sam-vit-base`). A model choice. | `gallery_sam_model` |
| `ODYSSEUS_GROUNDING_MODEL` | `routes/gallery/gallery_routes.py:95` | Grounding/detection model id for the gallery (default `google/owlvit-base-patch32`). A model choice. | `gallery_grounding_model` |
| `ODYSSEUS_STT_ENABLED` | `services/stt/stt_service.py:45` | Whether speech-to-text is enabled. **Already migrated**: read through `get_user_setting("stt_enabled", ...)` with the env var only supplying the fallback default for accounts that haven't set it. | *(already: `stt_enabled`)* |
| `ODYSSEUS_STT_DEFAULT_PROVIDER` | `services/stt/stt_service.py:56` | Default STT provider. **Already migrated**: fallback default behind `get_user_setting("stt_provider", ...)`. | *(already: `stt_provider`)* |
| `ODYSSEUS_STT_MODEL` | `services/stt/stt_service.py:62` | Default Whisper model size. **Already migrated**: fallback default behind `get_user_setting("stt_model", ...)`. | *(already: `stt_model`)* |
| `ODYSSEUS_STT_BEAM_SIZE` | `services/stt/stt_service.py:71` | Whisper decoding beam size. A quality/speed preference independent of hardware. | `stt_beam_size` |
| `ODYSSEUS_STT_MAX_AUDIO_SECONDS` | `services/stt/stt_service.py:73` | Max accepted audio duration for one STT request (default 300s). A limit. | `stt_max_audio_seconds` |
| `ODYSSEUS_TTS_CACHE_MAX_BYTES` | `services/tts/tts_service.py:47` | TTS output cache size budget (default 500 MB). A disk-budget preference. | `tts_cache_max_bytes` |
| `ODYSSEUS_TOOL_OUTPUT_INLINE_LIMIT` | `src/tool_output_store.py:110` | Tool-output size kept inline vs. offloaded, per context profile. **Already migrated**: `src/context_profiles.py` maps this to the `tool_output_inline_limit` UI setting; env is honored only for pre-existing installs that set it before the Context tab existed. | *(already: `tool_output_inline_limit`)* |
| `ODYSSEUS_TOOL_OUTPUT_HEAD_CHARS` | `src/tool_output_store.py:118` | Same mechanism, head-truncation length. **Already migrated** — see above. | *(already: `tool_output_head_chars`)* |
| `ODYSSEUS_TOOL_OUTPUT_TAIL_CHARS` | `src/tool_output_store.py:125` | Same mechanism, tail-truncation length. **Already migrated** — see above. | *(already: `tool_output_tail_chars`)* |
| `ODYSSEUS_VAULT_DATE_ORDER` | `src/vault_markdown.py:82` | How to read an ambiguous day/month date in a vault filename (default `day`). A locale-style preference. | `vault_date_order` |
| `ODYSSEUS_VAULT_SCAN_SECONDS` | `src/vault_scan.py:229` | Vault re-scan interval (0 disables). The rule's own "a cadence" example. | `vault_scan_seconds` |
| `ODYSSEUS_MLX_IMAGE_VLM_MODEL` | `scripts/mlx_image_server.py:299` | VLM model id for the standalone MLX image server script. Normally supplied as a `--vlm-model` CLI arg by the generated Cookbook runner; the env var is only a fallback for manual/direct invocation. Low-priority migration — belongs to the Cookbook model-serving capability, not a general setting. | `cookbook_mlx_image_vlm_model` |

## Ambiguous — flagged rather than forced (5)

The task rule ("would the same person want this different only because the
box is different?") does not resolve cleanly for these. Each depends on
*hardware or resource facts of the host* as much as on taste, which is exactly
where PLACEMENT and CHOICE overlap.

| Variable | Read at | What it controls | Why it's ambiguous |
|---|---|---|---|
| `ODYSSEUS_LOCAL_MODEL_GATE` | `src/llm_core.py:26` | Whether local-model traffic is serialized (foreground chat takes priority over background workloads on one local-model slot). Default on. | The "right" answer depends on whether this host actually has a scarce, single local GPU/model to protect (a deployment fact) — but an advanced user might also want to force it off as a personal choice on a beefier box. Leaning CHOICE if forced, since flipping it never widens any security surface and only trades latency characteristics. |
| `ODYSSEUS_FASTEMBED_LANE` | `src/embedding_lanes.py:38` | Historically: whether the local FastEmbed lane was built alongside a working custom embedding endpoint. **Now inert** — `build_embedding_lanes()` returns the single `fastembed` lane regardless, so `fastembed_lane_mode()` is read by nothing that acts on it. | Moot while it has no effect. The open question is not where it belongs but whether the remote lane comes back; until it does, the variable is a stub and this row is a note to whoever resurrects it. |
| `ODYSSEUS_STT_DEVICE` | `services/stt/stt_service.py:69` | Inference device for local Whisper (`cpu`/`cuda`, default `cpu`). | Directly tied to what accelerator hardware this host has — a placement fact — but a user might deliberately force `cpu` even with a GPU present, for stability or to leave the GPU free for chat. Leaning PLACEMENT if forced. |
| `ODYSSEUS_STT_COMPUTE_TYPE` | `services/stt/stt_service.py:70` | Whisper quantization/compute type (default `int8`). | Practical values are constrained by the device above (`int8` on CPU, `float16`/`int8_float16` on GPU) — a hardware fact — layered with a quality/speed preference on top. Leaning PLACEMENT if forced, paired with `STT_DEVICE`. |
| `ODYSSEUS_STT_CONCURRENCY` | `services/stt/stt_service.py:39` | Max concurrent STT inference slots (default 1). | Bounded by this host's CPU/GPU capacity, not by what the user wants — but framed as a tunable limit like the RAG/upload knobs. Leaning PLACEMENT if forced, since raising it on an underpowered host degrades rather than personalizes. |

## Not real environment variables — grep noise (38)

The grep in the task brief matches any `ODYSSEUS_[A-Z0-9_]+` token in the
`.py` sources, which also catches three things that are not application
configuration and would misrepresent the inventory if given a PLACEMENT or
CHOICE verdict:

1. **Bash-script-local variable names.** Cookbook's model-serving routes
   (`routes/cookbook_routes.py`, `routes/cookbook_helpers.py`,
   `routes/shell_routes.py`) build shell scripts as Python string literals
   (`runner_lines.append("ODYSSEUS_SERVE_CMD='...'")`) and ship them to a local
   or remote host to run. The names inside those strings are local variables
   *of the generated shell script*, scoped to that one script's process. They
   are never read via `os.environ` by Odysseus itself, and setting an actual
   environment variable with the same name on the Odysseus host would do
   nothing.
2. **Output-parsing markers.** `src/agent_tools/subprocess_tools.py` builds a
   per-command marker string (`__ODYSSEUS_CMD_START_{stamp}__`) to find a
   subprocess's output boundaries; the regex in the task brief matches the
   static prefix.
3. **A hardcoded constant that merely looks like an env-var read.**
   `routes/email_routes.py:70` assigns `ODYSSEUS_MAIL_ORIGIN = "odysseus-ui"`
   directly — it is a Python module constant, never read from the process
   environment at all.

None of these need migration; they aren't configuration. Listed here only so
the 123-token count in the grep is fully accounted for.

| Token | Where it appears | What it actually is |
|---|---|---|
| `ODYSSEUS_CMD_END_` | `src/agent_tools/subprocess_tools.py:128` | Prefix fragment of a per-command output-end marker (`__ODYSSEUS_CMD_END_{stamp}__:`), not a variable. |
| `ODYSSEUS_CMD_START_` | `src/agent_tools/subprocess_tools.py:127` | Prefix fragment of a per-command output-start marker. |
| `ODYSSEUS_CMD_EXIT` | `routes/cookbook_helpers.py:832` | Bash-script-local: exit code capture in a generated Cookbook download runner. |
| `ODYSSEUS_DDCOLOR_BIN` | `routes/cookbook_routes.py:2590` | Bash-script-local: resolved DDColor binary path in a generated MLX image-serving runner. |
| `ODYSSEUS_DDCOLOR_DIR` | `routes/cookbook_routes.py:2592` | Bash-script-local: `dirname` of the above. |
| `ODYSSEUS_DIFFUSION_CMD_PY` | `routes/cookbook_routes.py:2627` | Bash-script-local: the launch Python interpreter path in a generated diffusion-serving runner. |
| `ODYSSEUS_DIFFUSION_IMPORT_ERROR` | `routes/cookbook_routes.py:2638` | Bash-script-local: captured import-check error text in the same runner. |
| `ODYSSEUS_EXPECTED_MODEL` | `routes/cookbook_routes.py:132` | Bash-script-local: expected model id passed to a health-check poll script. |
| `ODYSSEUS_HF_CLI` | `routes/cookbook_routes.py:1246` | Bash-script-local: resolved `hf`/`huggingface-cli` path in a generated download runner. |
| `ODYSSEUS_INPAINT_BIN` | `routes/cookbook_routes.py:2610` | Bash-script-local: resolved inpaint binary path. |
| `ODYSSEUS_INPAINT_DIR` | `routes/cookbook_routes.py:2612` | Bash-script-local: `dirname` of the above. |
| `ODYSSEUS_MAIL_ORIGIN` | `routes/email_routes.py:70` | Hardcoded Python constant (`"odysseus-ui"`), not read from the environment. Used to tag outbound mail with `X-Odysseus-Origin`. |
| `ODYSSEUS_MLX_CMD_PY` | `routes/cookbook_routes.py:2428` | Bash-script-local: launch Python path in a generated MLX-LM serving runner. |
| `ODYSSEUS_MLX_IMAGE_BIN_DIR` | `routes/cookbook_routes.py:2550` | Bash-script-local: bin directory derived from `ODYSSEUS_MLX_IMAGE_CMD_PY`. |
| `ODYSSEUS_MLX_IMAGE_CMD_PY` | `routes/cookbook_routes.py:2539` | Bash-script-local: launch Python path for MLX image serving. |
| `ODYSSEUS_MLX_IMAGE_MODEL` | `routes/cookbook_routes.py:2556` | Bash-script-local: model id used to branch runner logic (HiDream/Boogu/DDColor/etc). Distinct from the real `ODYSSEUS_MLX_IMAGE_VLM_MODEL` (see CHOICE table). |
| `ODYSSEUS_MLX_IMPORT_ERROR` | `routes/cookbook_routes.py:2439` | Bash-script-local: captured import-check error text. |
| `ODYSSEUS_OLLAMA_CONTAINER` | `routes/cookbook_routes.py:361` | Bash-script-local: detected `ollama-rocm`/`ollama-test` container name. |
| `ODYSSEUS_OLLAMA_HOST` | `routes/cookbook_routes.py:2267` | Bash-script-local: bind host for a generated `ollama serve` runner. |
| `ODYSSEUS_OLLAMA_PORT` | `routes/cookbook_routes.py:2268` | Bash-script-local: bind port for the same runner. |
| `ODYSSEUS_OLLAMA_PULL_CMD` | `routes/cookbook_routes.py:358` | Bash-script-local: resolved `ollama pull`/`docker exec ... ollama pull` command. |
| `ODYSSEUS_OLLAMA_URL` | `routes/cookbook_routes.py:2287` | Bash-script-local: composed from the host/port locals above. |
| `ODYSSEUS_PATH__` | `routes/cookbook_helpers.py:362` | Substring of the marker `"__ODYSSEUS_PATH__%s"`, used to extract a captured `$PATH` value from a login-shell probe. Not a variable. |
| `ODYSSEUS_PREFLIGHT_EXIT` | `routes/cookbook_helpers.py:794` | Bash-script-local: preflight exit-code accumulator used throughout every generated Cookbook serving runner. |
| `ODYSSEUS_PY` | `routes/cookbook_routes.py:1222` | Bash-script-local: resolved `python3`/`python` path in a generated runner. |
| `ODYSSEUS_GITHUB_APP_` | `src/agent_worktree/diagnostics.py:168` | Regex artifact of the glob-style reference `ODYSSEUS_GITHUB_APP_*` in a hint string; the three real variables are `ODYSSEUS_GITHUB_APP_ID`/`_INSTALLATION_ID`/`_PRIVATE_KEY_PATH` (see PLACEMENT table). |
| `ODYSSEUS_SERVE_CMD` | `routes/cookbook_routes.py:2311` | Bash-script-local: the model-serve launch command, already chosen per-request via the Cookbook UI/API — not a deployment or general setting. |
| `ODYSSEUS_SERVE_PORT` | `routes/cookbook_routes.py:131` | Bash-script-local: serve port used by a generated health-check poll script. |
| `ODYSSEUS_SGLANG_CMD_PY` | `routes/cookbook_routes.py:2404` | Bash-script-local: launch Python path in a generated SGLang serving runner. |
| `ODYSSEUS_SGLANG_IMPORT_ERROR` | `routes/cookbook_routes.py:2421` | Bash-script-local: captured import-check error text. |
| `ODYSSEUS_TMUX` | `routes/cookbook_routes.py:259` | Bash-script-local: resolved `tmux` binary path for a generated remote-session wrapper. |
| `ODYSSEUS_TOKEN_FILE` | `src/agent_tools/claude_code_tools.py:329` | Regex artifact: the real variable is `CLAUDE_CODE_ODYSSEUS_TOKEN_FILE` (different prefix), already exposed as a UI setting ("Settings > Tools > Claude Code > Callback token file"). |
| `ODYSSEUS_USER_PATH` | `routes/cookbook_helpers.py:362` | Bash-script-local: captured `$PATH` from the user's login shell, for a generated remote-exec wrapper. |
| `ODYSSEUS_USER_SHELL` | `routes/cookbook_helpers.py:360` | Bash-script-local: resolved `$SHELL` in the same wrapper. |
| `ODYSSEUS_VLLM_BIN` | `routes/cookbook_helpers.py:815` | Bash-script-local: resolved `vllm` CLI path in a generated runner. |
| `ODYSSEUS_VLLM_HELP_CMD` | `routes/cookbook_routes.py:2313` | Bash-script-local: captured `vllm --help` output used to detect flag support. |
| `ODYSSEUS_VLLM_SUPPORTS_SWAP` | `routes/cookbook_routes.py:2324` | Bash-script-local: boolean derived from the help-text check above. |
| `ODYSSEUS_VLLM_VERSION` | `routes/cookbook_helpers.py:821` | Bash-script-local: captured `vllm --version` output, logged for diagnostics. |

---

## For contributors: where does a new option go?

Ask: *if I copied this exact install to a different machine, would I want
this different only because the machine is different?*

- Yes, and it's a path/port/URL/secret/trust decision → add it as an
  `ODYSSEUS_*` environment variable, document it in `.env.example` with a
  one-line comment and a safe default, and read it with `os.environ.get`.
- No, it's about what the software *does* regardless of host → add it to
  `src/settings.py` (`DEFAULT_SETTINGS`) and `src/settings_schema.py` so it
  gets a UI control. If a container deployment might need to pin the value
  (because it's derived from a bind mount, for instance), give the setting an
  `env_override` the way `vault_directory` does, rather than making the whole
  thing an environment variable.
- Genuinely both, depending on topology → make it a setting with an
  `env_override`, following the `vault_directory` / `ODYSSEUS_PERSONAL_DIR`
  pattern.

Do not add a plain `ODYSSEUS_*` variable for something a user would tune
per-taste (a model name, a limit, a cadence) just because that's the fastest
way to wire it up today — that's exactly how this list grew to 123 entries
with only a small subset represented in the UI.

## Migrating existing SQLite notes

Notes created by current builds are Markdown files under the configured vault.
Older installations may still have note rows in SQLite. Preview the conversion
first; the default command is read-only and prints every target path:

```bash
python -m src.notes_vault_migration
```

After reviewing the plan, apply it with `--apply`. The source rows are retained
and the command writes a manifest beside the application data, so the operation
can be undone with `--rollback`. Rollback removes only files recorded in the
manifest whose hashes are unchanged; a note edited afterward in Obsidian is
left in place.

```bash
python -m src.notes_vault_migration --apply
python -m src.notes_vault_migration --rollback
```

# Integrations, skills and self-hosting: architecture review (2026-10-01)

Two questions from the owner. Does the core need to bundle skills for tooling
that is not built in? Could those skills ship with their integration instead?
Short answers: no, and yes. The repository already has three partial versions of
the needed unit; this page proposes merging them into one.

Terms follow the codebase-design vocabulary: a **module** has an **interface**
(everything a caller must know) and sits at a **seam**; an **adapter** fills a
seam; a module is **deep** when a lot of behaviour sits behind a small
interface.

## What exists today

### Integrations are spread over four lists and three registries

Adding one built-in MCP integration means editing at least four hand-kept lists:
`builtin_mcp._BUILTIN_SERVERS` (or `_BUILTIN_NPX_SERVERS`, or
`github_mcp_servers()`), `builtin_mcp.BUILTIN_CATALOG`,
`mcp_manager._BUILTIN_FUNCTION_CALLING_SERVERS` and `mcp_manager.is_builtin()`.
A skill adds `builtin_skills._BUNDLED_SKILLS`; binaries add Dockerfile and
compose lines.

Three registries each hold part of what an integration is:

| Registry | Holds | Missing |
|---|---|---|
| `BUILTIN_CATALOG` (`src/builtin_mcp.py`) | name, status, the env var it needs | a toggle, health, skills, settings |
| `capabilities` (`src/capabilities.py`, `capabilities_builtin.py`) | enable flag, requirement probes, tools withheld when unavailable, settings shown | declared only for vault, code delegation, model serving, skill toolchain, host docker, worktrees and git; nothing for Todoist, Penpot, GitHub, browser, email, Pi, api_call, CalDAV or search |
| Plugin catalog (`src/plugin_catalog.py`, `data/plugins/*.json`) | a declarative bundle of skills, MCP servers, tools and models applied to a chat | it can only reference things that already exist; no manifests exist yet |

`src/integrations.py` (`api_call` presets) and CalDAV are a fourth and fifth
mechanism with their own settings and UI.

### Coupling, with Penpot as the example

Penpot's own code is self-contained: `src/penpot_studio.py`, `penpot_svg.py`,
`penpot_text.py`, `mcp_servers/penpot_studio_server.py`, the skill, and
`docker/penpot/`. Nine core places still know about it: the four lists above,
`_BUNDLED_SKILLS` with four hard-coded `mcp__penpot_studio__*` names,
`preview_tools.py` importing `penpot_text.browser_executable`, a
`discover_tools` example, and the shipped loadout, which hard-codes a user
server id (`mcp__c5ec6d7a__*`) that exists on one machine only.
`penpot_studio.py` also finds its credentials by searching the user's MCP table
for a row whose name contains "penpot". Todoist and the Pi worker follow the
same pattern; Claude Code delegation touches about 50 files, because it is a
core feature (workers, approvals, the UI) as well as an integration.

### Skills for integrations are gated wrongly in both directions

A skill reaches the model through three paths (`website/agent-runtime.md` and
the skills map behind this review):

| Path | Filter today |
|---|---|
| A. The agent loop's skills index | `requires_toolsets` checked against `TOOL_SECTIONS` keys, which are native tool names only |
| B. The chat route's skills index (Agent mode) | none |
| C. "Relevant skills" matched by keywords, procedure inlined | none |

So `penpot-design-workflow`, `local-pi-delegation` and `claude-code-delegation`
are hidden from path A even when the integration is connected, listed by path B
on a deployment that has none of them, and inlined by path C whenever a keyword
matches.

### What a self-hoster gets

- Todoist and Penpot Studio connect with no credentials and show "Running" in
  Settings > Built-in; every call then fails with "not configured".
- Bundled skills carry one person's setup. `local-pi-delegation` names Windows
  `D:/` paths and an "AI Mind" vault folder; `claude-code-delegation` has an AI
  Mind record step; `harness-context-and-tool-routing` names Todoist and Lotus.
- The default image installs the Todoist CLI, `github-mcp-server`, a Docker CLI,
  Chromium and the JDK/Maven toolchains whether or not they are used.
- Prompts mention Jellyfin, which has no `api_call` preset.

## Decision

Make the **integration** a first-class module. An integration package declares
everything Odysseus needs to know about it in one manifest, and the core derives
its lists, health, settings, skill visibility and loadout templates from the
manifests instead of from hand-kept lists.

The seam is real, not hypothetical: it already has three kinds of adapter today
(Python MCP servers, npx/binary MCP servers, native tool providers such as
Cookbook and Claude Code) plus user-added MCP servers.

### The interface

```
integrations/<id>/
  integration.json   id, name, kind (mcp-python | mcp-npx | mcp-binary | native),
                     requirements (env, settings, binaries, services),
                     settings (keys and schema), tools (or "from server"),
                     prompt (short routing text, or "from server instructions"),
                     skills: [dirs], loadouts: [templates], health probe
  skills/<name>/SKILL.md
  loadouts/<name>.json   templates that name tools by integration id, never by hash
  server.py | README.md
```

`integrations/claude` and `integrations/codex` today hold something else: skills
that teach outside agents to call Odysseus. They would move to `clients/`.

A caller (the agent loop, the tool policy, the Settings UI) asks the registry
three things: which integrations are enabled and healthy, which tools and skills
they contribute this turn, and what their health is. Everything else stays
inside the module.

### Rules that follow

- **Skills belong to the integration that makes them useful.** An integration's
  skills are installed with it and are visible only while it is enabled and
  healthy (`requires_integration: penpot`), in all three skill paths. Core
  bundles only skills that use core tools.
- **Credentials belong to the integration.** Penpot stops borrowing env from a
  user MCP row by name.
- **Loadouts reference integrations, not hashes.** A template says
  `penpot-mcp:*`; import maps it to whatever id that server has on the target
  install.
- **Server instructions are used.** MCP servers can send `instructions` at
  initialisation; Odysseus drops them today. They become an integration's
  prompt text, as untrusted data like the rest of a server's descriptions.
- **The plugin catalog becomes the package format for user-added
  integrations.** v2 adds an MCP server spec, prompt text, skills and loadout
  templates to the v1 references. Install stays admin-approved; nothing runs
  in-process.

### Which bundled skills stay in core

| Skill | Today | Proposed home |
|---|---|---|
| penpot-design-workflow | bundled, wrongly gated | Penpot integration |
| claude-code-delegation | bundled, wrongly gated | Claude Code integration (with the AI Mind step made optional) |
| local-pi-delegation | bundled, personal paths | the owner's own skill, or a Pi integration with paths from settings |
| todoist-planning, todoist-retrospective | in the repo, never installed | Todoist integration |
| learning-coach | in the repo, never installed | an optional pack, or the owner's skills |
| visual-asset-sourcing | bundled | core (works with web access; uses Penpot's icon search when present) |
| harness-context-and-tool-routing | bundled | core, with the Todoist and Lotus lines supplied by those integrations |
| grilling, unslop, writing-for-agents, diagnosing-bugs, codebase-design, domain-modeling, improve-codebase-architecture, triage, resolving-merge-conflicts | bundled | core (they use core tools only) |

### Alternatives considered

- **Keep bundling and fix the gating only.** Cheapest, and phase 1 below does
  this anyway, but the four lists and the per-machine loadout stay, and every
  new integration keeps touching core files.
- **Everything external as plugins or MCP servers.** Email, the vault, notes and
  calendar have native tools, UI and storage in core; pushing them out would add
  a seam with one adapter and no gain.

## Plan

1. **Fix what is wrong now** (no new structure):
   - One skill filter for paths A, B and C, built from the turn's actually
     available tools (native, connected MCP, delegation providers) plus the
     loadout scope.
   - The frontmatter parser reads the nested `metadata:` block; today
     `category`, `status` and `source` there are ignored.
   - Usage and audit records for bundled skills use the same key on write and
     read (`owner::name` vs `name`), so audits stop re-picking them.
   - The shipped-skill tool-gate exemption looks at the skills actually shown,
     not every skill the owner has.
   - Built-in status reports "not configured" when credentials are missing.
   - Jellyfin leaves the prompts until it has a preset.
   - One tool-alias table instead of two (`skill_toolsets.py` and
     `agent_loop.py`).
2. **Integration manifests for the built-ins**, read by one registry that
   generates the four lists, the capability entries and the Settings > Built-in
   rows. Code stays where it is.
3. **Move integration skills and loadout templates into their packages**,
   switch gating to `requires_integration`, and remap loadout server ids on
   import.
4. **Plugin catalog v2** for user-added integrations, with server
   instructions and attached skills.
5. **Optional image slimming**: build arguments for integration binaries
   (Todoist CLI, GitHub MCP, Docker CLI, toolchains).

Phase 1 is independent and fixes visible bugs. Phases 2 and 3 can go one
integration at a time, starting with Penpot, which has the most self-contained
code.

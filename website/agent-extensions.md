# Agent personalities, plugins and portable skills

## Per-agent instructions

Open **Agents → Agent loadout → General → Personality & instructions**, edit,
then **Save loadout**. The text is stored with that agent's chat, not in the
global prompt cache. It applies on later/reopened turns too. Applying a preset
copies its instructions: later edits to the reusable preset do not alter
existing agents. Clearing this field removes only that agent's customization.
The shared platform safety and permission rules still apply to every agent.

## PromptScript and skills.sh are different tools

PromptScript (`prs`) compiles portable instructions. The skills.sh installer is
the separate `skills` CLI. Both are pinned in `package-lock.json`; the image
installs them with lifecycle scripts disabled. Native setup:

```sh
npm ci --ignore-scripts
npm run promptscript -- --version
npm run skills:cli -- --version
```

The skills installer requires Node **22.20 or newer**. The pinned image Node
release meets this requirement. No personal/global skill directory is changed
by installing the CLI packages. Telemetry is disabled in the image and the
project installer helper. For direct native CLI calls, set
`DISABLE_TELEMETRY=1` and `PROMPTSCRIPT_TELEMETRY=false` if desired.

### Fixing “PromptScript does not support global skill installation”

Run from the project in which you want the skill:

```sh
npm run skills:add -- vercel-labs/skills find-skills --dry-run
npm run skills:add -- vercel-labs/skills find-skills
npm run skills:list
```

The helper selects **only PromptScript**, uses copies rather than symlinks,
rejects global/all-agent flags and refuses to overwrite an existing skill.
It prompts normally; add `--yes` only after reviewing the source. Equivalent
when the CLI is on PATH (for example inside the image):

```sh
skills add vercel-labs/skills --skill find-skills --agent promptscript --copy
```

Do **not** add `--global` / `-g` for PromptScript. Existing global installs for
other agents are unaffected. In Docker, use a persistent project/workspace
directory; the application image's filesystem is not persistent storage.

PromptScript discovers `.promptscript/skills/<name>` and `.agents/skills/<name>`.
Initialize only the project/targets you intend to configure; preview first:

```sh
npm run promptscript -- init --yes --targets claude --no-hooks --dry-run
npm run promptscript -- compile --dry-run
```

Initializing/compiling foreign instruction files does **not** automatically
replace Agamemnon's system prompt, install hooks, or load a plugin.

## Import a portable skill into Agamemnon

In **Memory → Skills → Add Skill**, paste a specific GitHub skill folder or
`https://skills.sh/<owner>/<repo>/<skill>`. Preview every file, then choose
**Import as draft**. The reviewed snapshot (not a later remote revision) is
installed for the signed-in user. Previews expire after ten minutes or restart;
re-preview if necessary. The cache is bounded and process-local (a deployment
with multiple API workers needs sticky requests or a future shared review store).

No script or model audit runs during import. Imported drafts cannot enter the
automatic skill index or matched-skill context. Review/edit, then explicitly
publish or request an audit. Publishing makes instructions eligible; it never
grants tools, MCP connections, shell execution, memory writes or private vault
access. Skills use the existing per-agent skill selection and lazy reference
loading, not a new full-catalog prompt injection.

For operator-managed local bundles already installed by a CLI:

```sh
python scripts/odysseus-skills inspect-local .promptscript/skills/find-skills --pretty
python scripts/odysseus-skills import-local .promptscript/skills/find-skills --owner YOUR_USERNAME --reviewed
```

This is a local **administrator utility**, not an agent tool or authentication
boundary. Never expose it as a multi-user API. Select a single real directory;
symlinks/junctions, oversized and unsupported binary resources fail clearly.
The ordinary import UI supports text resources only; binary-backed skills need
operator review/setup. Original upstream SKILL.md is retained as
`IMPORTED_SOURCE.md` when Agamemnon normalizes its metadata/body.

## Declarative plugins (v1)

Open an agent's loadout and expand **Plugins**. An administrator can import
a JSON manifest there, then each agent can independently preview and apply
the plugin. Example:

```json
{
  "schema_version": 1,
  "id": "research-kit",
  "name": "Research kit",
  "version": "1.0.0",
  "description": "Approved research tools grouped for an agent",
  "capabilities": {
    "tools": ["web_search", "fetch_url"],
    "skills": [],
    "mcp_servers": [],
    "models": []
  }
}
```

Only existing capabilities can be applied: skills visible to that owner, known
tool names, configured MCP server IDs and known model names. References are
grouped into the agent's selection; existing access modes, disabled tools,
approval gates and private-note rules continue to win. A plugin cannot switch
`none` to `all`, enable a globally disabled tool or create MCP credentials.

Manifests live under `data/plugins`; the catalog is shared administrator-managed
configuration, **not shared personality**. Applied references are snapshots:
editing a manifest does not silently change running agents; explicitly reapply
to update. Removing a selection removes only plugin contributions, preserving
unrelated manual choices. Deleting the catalog manifest is not a global revoke
of already-applied snapshots; remove it from each agent or use the existing
global tool/connection disable controls for immediate revocation.

This first version deliberately accepts no executable entrypoints, install
commands, hooks or arbitrary remote packages. It is an Agamemnon capability-bundle
format, not a claim of compatibility with every vendor's plugin format.

## Plugins as integration packages (schema v2)

Schema v2 keeps everything above and adds an optional `integration` block, so a
plugin can package a user-added integration: an MCP server, a few lines of prompt
text, skills and loadout templates. v1 manifests load unchanged.

```json
{
  "schema_version": 2,
  "id": "demo",
  "name": "Demo pack",
  "version": "1.0",
  "capabilities": {},
  "integration": {
    "name": "Demo",
    "mcp_server": {
      "name": "Demo Server",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "demo-mcp"],
      "env": [{"name": "DEMO_TOKEN", "description": "API token", "required": true}]
    },
    "instructions": "Call demo_list before demo_get.",
    "skills": [{"name": "demo-howto", "content": "---
name: demo-howto
description: ...
---
..."}],
    "loadout_templates": [{"format": "odysseus-agent-profiles", "version": 1,
      "profiles": [{"name": "Demo agent", "instructions": "...", "allowed_mcp_servers": ["{server:Demo Server}"]}]}]
  }
}
```

- `mcp_server.transport` is `stdio` (command and args), `http` or `sse` (a URL
  without credentials). `env` is a list of variable names with descriptions.
  A manifest never contains values: a `env` given as an object, or an entry with
  a value, is rejected, and so are loader variables such as `PATH` or `LD_PRELOAD`.
- `instructions` is at most 4 KB. `skills` are SKILL.md bodies (up to 10, 32 KB
  each). `loadout_templates` are `odysseus-agent-profiles` v1 documents; a
  `{server:<name>}` reference in `allowed_mcp_servers`, `enabled_tools` or
  `disabled_tools` resolves to the server this install created.
- A v2 manifest can be up to 256 KB (v1 stays at 64 KB).

### Install and uninstall

Only an administrator installs, and nothing runs until they do. The Plugins panel
shows the command (or URL), the environment names, the skills and the templates,
then asks for the values.

`POST /api/plugins/{id}/install` takes `{"env": {NAME: value}, "approved":
"<command line or URL>", "publish_skills": false}`. `approved` must equal the line
the admin was shown, so a manifest replaced after the page rendered cannot run
something unread. The call:

1. creates the MCP server through the same handler as `POST /api/mcp/servers`
   (env values go only into that server row, never the manifest, the install
   record or the response);
2. imports each skill as the admin's own skill with `source: imported`, status
   `draft` unless `publish_skills` is true, gated on the server being connected;
3. imports the templates through profile import, after resolving `{server:<name>}`,
   renaming a loadout that would overwrite an existing one;
4. records what it created in `data/plugins/installed/<id>.json` and returns the
   server status, the skills, the loadouts and any unresolved references.

A failure in step 2 or 3 removes what steps 1 to 3 had already made.
`DELETE /api/plugins/{id}/install` removes exactly the recorded server, skills and
loadouts, and nothing the admin created by hand. Deleting the manifest of an
installed plugin is refused until it is uninstalled. Enabling the plugin for a
chat grants its installed server in the chat's MCP allowlist, subject to the same
selected-allowlist rules as a v1 server reference.

### Safety stance

No code runs in-process and nothing installs packages. An `stdio` command is
whatever the administrator approves after reading it verbatim; Agamemnon spawns
it as it would any hand-added server. Skill, template and instruction text from
a package is untrusted: skills land as drafts, and instructions are shown only
as labelled context (below).

### Server instructions

An MCP server can send `instructions` when it initialises. Agamemnon keeps up to
4 KB of it per server and prints it, with a plugin's `instructions`, directly
under that server's name in the MCP tool block of the prompt, labelled as
untrusted text and only while the server's tools are offered that turn. A server
whose tools are all switched off, or whose connection is gone, contributes
nothing. The text is background on the tools, not commands; the model still
follows the chat's own instructions and approval rules.

The registry treats an installed plugin with a server as an integration of kind
`plugin`: it counts as available while its server is connected, and its tools
map back to it (`requires_integration: <plugin id>` on its skills).

Sources: [PromptScript CLI](https://getpromptscript.dev/latest/reference/cli/),
[portable skill directories](https://getpromptscript.dev/latest/guides/npx-skills/),
[skills CLI](https://www.skills.sh/docs/cli).

## Verification (2026-09-17)

- Focused Python regression selections: 405 passed, 5 skipped (environment/platform).
- Four Node installer/plugin UI contract tests passed; modified JavaScript syntax
  and Python compilation checks passed.
- Native PromptScript 1.18.1 initialization, validation and compile preview passed
  in an isolated temporary project; skills CLI 1.6.0 ran successfully. No existing
  project instructions or global skill installs were overwritten.
- Real read-only retrieval of the reported `vercel-labs/skills/find-skills` bundle
  succeeded. npm production-dependency audit reported zero vulnerabilities.
- Docker engine was unavailable on this host: container build and post-deploy
  browser/provider acceptance remain outstanding. Unit UI tests are not visual QA.

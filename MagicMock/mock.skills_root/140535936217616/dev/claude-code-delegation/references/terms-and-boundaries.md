# Terms and boundaries for the Claude Code integration

Summary of Anthropic's published position (code.claude.com/docs/en/legal-and-compliance
and the Agent SDK overview, read 2026-09-10) as it applies to Odysseus. This is
a working note for the harness, not legal advice; the linked pages are the
authority and change over time.

## What this integration does

- Runs the **unmodified** `claude` binary, headless (`-p`), inside an approved
  Git checkout, as a subprocess of Odysseus.
- The binary authenticates itself with the operator's own credentials: a
  Claude subscription sign-in performed by the operator, or an
  `ANTHROPIC_API_KEY` the operator provides in the container environment.
- Odysseus never reads, stores, forwards, or proxies those credentials. The
  only secret it hands the child is a scoped **Odysseus** token (optional, via
  `claude_code_odysseus_token_file`) so Claude can call `/api/codex/*` back
  in this instance.

## Why that stays inside the published terms

- Running Claude Code "in your products or services (e.g. in hosted sandboxes
  or other agent infrastructure)" is expressly contemplated, with two
  conditions: the binary must not be modified, and each end user must
  authenticate with their own Anthropic API key, subscription credentials, or
  third-party provider credential — usage is billed to that user, not resold
  or intermediated. Odysseus meets both: it is a self-hosted, single-operator
  workspace whose operator signs in themselves.
- The docs explicitly allow "an end user … signing in to the unmodified
  Claude Code binary with their own Claude subscription, including where a
  platform hosts Claude Code".
- Advertised Pro/Max limits "assume ordinary, individual usage of Claude Code
  and the Agent SDK". A personal harness delegating the operator's own coding
  tasks is ordinary individual use; a fleet of always-on jobs is not.

## What would cross the line (do not build these)

- Extracting the OAuth/session token from Claude Code's config and calling
  the Anthropic API with it from Odysseus (for example, to add "claude" as a
  chat model). The docs forbid developers from collecting, storing, or
  intermediating Claude.ai credentials or routing requests through Free/Pro/
  Max plan credentials on their users' behalf.
- Offering Claude.ai login inside Odysseus's own UI, or sharing one
  subscription across several Odysseus users.
- Modifying or wrapping the binary in a way that removes or restricts any of
  its built-in authentication methods.
- Branding the feature as "Claude Code" in a way that suggests Anthropic built
  or endorses Odysseus. Saying plainly that Odysseus runs Claude Code is fine.

## If Odysseus should talk to Claude as a chat model

That is a different product path: configure an Anthropic **API key** as a
model endpoint (Commercial Terms, billed per token). Do not reuse the Claude
Code sign-in for it. The Agent SDK route ("Claude Code as a library") is the
same story: API-key authentication, not subscription credentials.

## Operational notes

- `--bare` (the recommended mode for scripts, and slated to become the `-p`
  default) never reads OAuth credentials. The runner does not pass it, so a
  subscription sign-in keeps working; when it becomes the default, the
  operator will need an API key in the child environment or the runner will
  need to opt out explicitly. `status` reports the binary version so this can
  be checked.
- `--restricted` (on by default via `claude_code_restricted`) ignores hooks
  and MCP servers declared inside the checkout, confines file tools to it, and
  refuses bypassPermissions. It does not affect authentication.

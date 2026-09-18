# Harness log review — September 17, 2026

Scope: the supplied 06:17:22–06:19:55 deployment log (200 lines), plus bounded
regression fixes. This is not a new execution sandbox or a full context rewrite.

## Evidence

- Patch and grep calls succeeded; Chroma/provider requests returned successful
  responses. The log does not show a crash or a context-limit failure.
- `git pull` was blocked before Git ran because the chat lacked the explicit
  private-vault grant required for unrestricted subprocesses. The response
  incorrectly described this as missing workspace filesystem access.
- “use your mcp tools” selected configuration tools and unrelated integrations;
  “you had the paths before!” did not inherit task context. The repository path
  itself was still present and recalled by the model.
- Tiny follow-ups used roughly 25k–30k estimated input tokens, including
  2.6k–2.8k schema tokens. Automatic personal-document search ran even on
  frustrated feedback. The editor document was loaded/logged, but the existing
  agent relevance gate excluded it from the prompt; do not count its entire
  body as proven prompt overhead.
- Ledger compaction changed an existing history prefix. The next round cached
  3,584 of 43,515 actual input tokens; subsequent stable rounds recovered strong
  reuse. Schema expansion coincided with another cache miss. This warrants
  measurement, not an assertion that all cache misses have one cause.

## Implemented

- Contextual corrections/method feedback inherit recent human task context;
  standalone new tasks do not. MCP administration requires management intent.
- Connected MCP tools remain discoverable but are not eagerly attached merely
  because their server is small. Explicit/relevant/recent bindings remain.
- Bounded feedback turns skip automatic document RAG and passive editor lookup.
  Explicit RAG opt-in, attachments and substantive grounding requests survive.
- Discovery, initial schemas and dispatch share the private-tool gate. Dispatch
  refreshes the session grant; revocation narrows access immediately and a new
  grant cannot silently elevate an already-running request.
- Structured permission errors identify the actual restriction, say no operation
  ran, and preserve dedicated public-file tooling.
- Missing sessions cannot carry caller-only private grants. Standard filesystem
  MCP names share the existing conservative private gate in discovery and
  dispatch; the tool surface was checked against the
  [official filesystem server reference](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem).

## Important remaining boundary

An unrestricted shell can access the mounted private vault regardless of its
working directory. Enabling **Allow private vault reads** is therefore a broad
private-data grant, not a repository-only switch. No grants were enabled by this
change. Supporting Git/tests without that grant requires a genuinely isolated
execution environment, separately scoped and validated.

The MCP name guard is defense in depth, not a sandbox for arbitrary third-party
servers. Operators must not expose private mounts through custom MCP wrappers
under other names. Enforced per-server isolation and trusted access classification
remain necessary before claiming general containment of custom integrations.

## Deployment checks

Local verification: **767 Python tests passed, 5 skipped**, plus **4 Node tests
passed** across the extension, routing, context, privacy, workspace and relevant
regression suites. Skips cover Windows symlink privileges and unavailable
`time.tzset`. Python compilation and `git diff --check` passed. This is a targeted
combined selection, not the full repository suite.

1. In a chat without private access, read a public repository file, then request
   Git pull. Public reading should work; shell should not be offered/executed,
   and the response should explain the precise restriction without losing paths.
2. Follow a concrete task with “use your MCP tools” and “you had the paths
   before!”; verify continuity and no automatic MCP administration calls.
3. Use a connected integration absent from the initial list; verify discovery
   loads it and the next round can execute it within the existing policy.
4. Revoke private access during a run; the next private-capable call must fail
   before dispatch. Run another agent concurrently to check session isolation.
5. Compare actual total/cached input, schemas, first-token latency and completion
   quality. Small initial catalogs may require a discovery round; unit tests do
   not establish real-provider savings or task-success rates.

Docker was unavailable on the development host, so no image rebuild or live
deployed-provider validation has been claimed. Deployment checks remain pending.

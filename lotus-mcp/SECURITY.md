# Security

## Threat model

Lotus holds a database of mood records and possibly journal notes, and it is
driven by a language model. That shapes what it defends against.

**Assets.** The SQLite database, the note text inside it, and the source files
in `imports/`.

**Trust boundaries.**

| Input | Trusted? | Why |
|---|---|---|
| `config.yaml` | Trusted | Written by the operator; mounted read-only |
| MCP tool arguments | **Untrusted** | Chosen by a model, which can be steered by content it read elsewhere |
| Import file bytes | **Untrusted** | Arbitrary content; may be hostile or merely broken |
| Cell values and note text | **Untrusted data, never instructions** | Never evaluated, never executed |

**Adversaries considered.**

1. *A model that has been prompt-injected.* It may ask for notes, ask for the
   whole history, ask for a file outside the import root, or ask to delete
   things. Every one of those is refused by the server, not by a prompt.
2. *A malicious or malformed import file.* Traversal-shaped filenames, symlinks,
   enormous fields, hostile encodings, deeply nested JSON, formula cells.
3. *A local process reading the database file.* Explicitly **not** defended
   against — the database is unencrypted. See PRIVACY.md.

**Out of scope.** A compromised host, a malicious operator, and physical
access. If someone can read `data/mood.db`, they have the data.

## Authentication and transport

Lotus speaks **MCP over stdio only**. The client that spawns the process is the
only thing that can talk to it. There is no listening socket, no port, no
bearer token, and therefore no token to leak, no default credential to forget
to change, and no health endpoint that could serve mood data.

**HTTP transport is deliberately not implemented.** An authenticated HTTP MCP
transport needs a credential store, constant-time comparison, rate limiting,
request-size caps, TLS termination decisions, and CORS handling. Shipping a
half-built version of that in front of journal data would be worse than
shipping none. If it is added later it must: bind `127.0.0.1` by default,
require a bearer token read from a mounted secret file (never a literal in
config, never a default value), never log the token, apply request-size and
rate limits, and keep health endpoints free of mood data.

For remote access to the NAS, use Tailscale, WireGuard, or a private Docker
network. Do not port-forward.

## Secret handling

Lotus currently requires no secrets: no accounts, no API keys, no tokens. The
`secrets/` mount exists so that a future need is met by a mounted file rather
than by an environment variable in a compose file that ends up in a screenshot.

Rules if that changes: secrets are read from files under `secrets/`, mounted
read-only; never committed; never logged; never returned by a tool; never
placed in the database.

Privacy settings are intentionally **not** environment-overridable. Loosening
consent requires editing a mounted config file that the container cannot write.

## Filesystem containment

Client-supplied paths are relative to the configured import root and are
resolved by `security.resolve_within_root`, which rejects:

- absolute paths under POSIX *or* Windows rules (`/etc/passwd`, `C:\…`, `C:foo`)
- UNC prefixes (`\\server\share`, `//server/share`)
- `..` traversal, including after separator normalization (`..\x` on Linux)
- NUL bytes and empty paths
- **symlink escape** — containment is verified *after* full symlink resolution,
  so a link inside the root pointing outside it is refused; a link that resolves
  inside the root is allowed

Beyond path shape: only regular files are opened (not directories, FIFOs or
devices), only `.csv` and `.json` extensions, and only below the configured
size limit. Filenames are sanitized before being stored or echoed, so a report
never reveals a host path.

Destructive operations (`wipe`, `rollback`, `redact-notes`, `rebuild`) exist
only in the CLI. They are not MCP tools, so no model can invoke them.

## Malicious import files

| Attack | Defence |
|---|---|
| Formula injection (`=HYPERLINK(...)`) | Imported values are inert text, never evaluated. Cells that Lotus *writes* are disarmed by `csv_safe_cell` |
| Oversized file | `max_file_bytes`, checked before opening |
| Oversized field / note | `max_field_chars`, `max_note_chars`, plus a 1 MiB CSV field cap |
| Too many records / columns | `max_records_per_file`, `max_columns` |
| Deeply nested JSON | Depth scanned on the raw text *before* `json.loads` builds anything |
| Malformed encoding | Strict UTF-8; a decode failure is a clean refusal, not mojibake |
| Duplicate column names | Refused outright — Lotus will not guess which column is the note |
| Ragged rows | Rows with more fields than the header are refused |
| Ambiguous timestamps | Rejected, never guessed; DST gaps and repeats rejected too |
| Unicode / emoji / multiline | Supported; newlines normalized so the same note hashes identically across platforms |
| Note content as instructions | Notes are stored and counted, never interpreted or executed |
| SQL injection via filters | All filters are bound parameters; the only interpolation is a run of `?` placeholders whose count comes from `len()` |

## Logging policy

- Logs go to **stderr**. stdout is the MCP channel and carries nothing else.
- Note text is never logged. A `logging.Filter` drops any record explicitly
  tagged `contains_note`, as a backstop behind call sites that already don't
  log notes.
- Exception messages are not passed through to clients. Database and parser
  errors are logged **by type only**; the client receives a fixed phrase.
  Pydantic validation errors are summarized to field names and error types,
  because pydantic embeds the offending input — which for a note would mean
  journal text in an error response.
- Import reports contain counts, structural field names, and sanitized
  basenames. No raw rows, no host paths, no stack traces.

## Container hardening

- Non-root user (UID 10001; `user:` in compose overrides it to match host
  ownership)
- Read-only container filesystem; writable mounts limited to `data/`,
  `imports/`, `logs/`
- `config/` and `secrets/` mounted read-only, so the process cannot rewrite the
  policy that constrains it
- `cap_drop: ALL`, `no-new-privileges:true`
- No published ports
- CPU and memory limits; log rotation
- Health check reports schema version and row counts only

## Safe deployment checklist

1. Put `LOTUS_HOST_DIR` on an **encrypted volume**. The database is not
   encrypted by Lotus.
2. `chown` the host directories to `LOTUS_UID:LOTUS_GID` and `chmod 750`.
3. Review `config.yaml` before first import. Leave `import_notes: false` unless
   you specifically need note text.
4. Do not publish ports. Do not add a reverse proxy in front of Lotus — there
   is nothing to proxy.
5. Dry-run every new file before importing it.
6. Verify `docker compose config` shows no `ports:` entry.
7. Consider pinning the base image by digest and running a container scan; the
   Dockerfile pins by tag (`python:3.12-slim`) for readability.

## Reporting a vulnerability

Report privately to the maintainer of this repository — do not open a public
issue for anything exploitable, and **never attach a real database, a real
export, or real journal text** to a report. A synthetic reproduction is always
sufficient; if it isn't, describe the shape of the data instead of including it.

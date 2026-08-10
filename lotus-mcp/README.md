# Lotus

A local-first, read-only MCP adapter that lets Odysseus reason about **mood
records you already own**, without any of that data leaving your machine.

Lotus reads CSV/JSON files you place in an approved directory, normalizes them
into a stable internal schema, stores them in local SQLite, and exposes a small
set of narrowly scoped MCP tools. It has no accounts, no tokens, and makes no
outbound network calls.

---

## What Lotus is not

Lotus is named after nothing in particular. It is **not** an integration with
the How We Feel app, and it does not talk to How We Feel or any other mood
service.

The adapter accepts a `how_we_feel_export` *label* on an import, because you
may well have typed up or reconstructed your check-ins from that app. That
label records where you believe the data came from. It does not mean Lotus
understands an official export format, because there is no such format to
understand.

### Capability matrix

| Access path | Status | In Lotus |
|---|---|---|
| Local CSV import | **Implemented and tested** | ✅ `manual_csv`, `how_we_feel_export` label |
| Local JSON import | **Implemented and tested** | ✅ `manual_json` |
| Configurable column mapping | **Implemented and tested** | ✅ `config/import_mapping.example.yaml` |
| Aggregate summaries | **Implemented and tested** | ✅ `mood.summarize_period` |
| Raw entry access | **Implemented, disabled by default** | ⚠️ policy-gated |
| Journal note storage | **Implemented, disabled by default** | ⚠️ policy-gated, off at import *and* at read |
| How We Feel official API / developer platform | **None found.** No public API or developer platform has been located, and the operator of this deployment independently reported (2026-08-10) that the app appears to offer no API access. | ❌ not implemented |
| How We Feel OAuth / personal access tokens | Unverified — no evidence any exist | ❌ not implemented |
| How We Feel webhooks | Unverified — no evidence any exist | ❌ not implemented |
| How We Feel native CSV/JSON export | Unverified — no export format has been confirmed | ❌ no vendor parser |
| Apple Health / Health Connect sync | Unverified for this app; not built | ❌ label accepted, import refused |
| Documented deep links / Shortcuts / Android intents | Unverified — no evidence any exist | ❌ not implemented |
| Reading the app's private local database | Out of scope by policy | ❌ never |
| Reverse engineering, traffic capture, screen scraping | Out of scope by policy | ❌ never |

**The file-based path is not a workaround pending an API.** As far as anyone
involved can determine, no API exists. Importing files you control is the whole
design, and every extension point below assumes that stays true unless a
vendor-documented interface is independently verified.

The sample schemas in `config/` and `tests/fixtures/` are **Lotus conventions**.
They are not, and do not claim to be, any vendor's export schema.

---

## Data model

Every imported record normalizes to:

```json
{
  "id": "locally-generated-id",
  "source": "manual_csv",
  "source_record_id": "optional-source-id",
  "occurred_at": "2026-08-10T09:15:00-04:00",
  "timezone": "America/New_York",
  "emotion_label": "overwhelmed",
  "emotion_family": "anxious",
  "valence": -0.6,
  "energy": 0.7,
  "intensity": 0.8,
  "note": "optional sensitive text",
  "tags": ["work", "planning"],
  "context": {}
}
```

Rules worth knowing before you prepare a file:

- **`occurred_at` is mandatory and must be unambiguous.** A timestamp with no
  timezone is *rejected*, not guessed at. If your data is naive local time, say
  so explicitly with `assume_timezone` in a mapping. Timestamps that fall in a
  daylight-saving gap or repeat are rejected too — they have no single answer.
- Supported ranges: `valence` −1…1, `energy` 0…1, `intensity` 0…1. A source on
  a different scale (say 1–5) is rescaled by a declared `scales:` block, never
  by inference.
- `emotion_label` is full Unicode, emoji included.
- Notes, tags, `source_record_id`, and the affect values are all optional.
- Unrecognised columns are dropped unless a mapping sets
  `retain_unknown_fields: true`, in which case they are kept as bounded flat
  strings in `context`.

---

## Local setup

Requires Python 3.11+ (3.12 recommended; that is what the container ships).

```bash
cd lotus-mcp
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp config/config.example.yaml config/config.yaml
export LOTUS_CONFIG=$PWD/config/config.yaml       # Windows: $env:LOTUS_CONFIG=...

lotus-mcp policy        # show the active consent boundary
lotus-mcp health        # verify the database initializes
```

Run the tests:

```bash
pytest -q
ruff check .
ruff format --check .
```

---

## Import workflow

1. Put a `.csv` or `.json` file in `imports/incoming/`.
2. **Dry run first.** It writes nothing and moves nothing.

   ```bash
   lotus-mcp validate moods-2026-08.csv --source-type manual_csv
   ```

   The report tells you which column fed which field, how many records would
   be inserted, how many are duplicates, and exactly which rows were rejected
   and why.

3. Commit it:

   ```bash
   lotus-mcp import moods-2026-08.csv --source-type manual_csv
   ```

4. Check what happened:

   ```bash
   lotus-mcp batches
   ```

On success the file moves to `imports/processed/`. On a validation failure it
moves to `imports/failed/` — the bytes are untouched, just relocated, so you
can fix and retry. A file is never modified in place and never overwritten.

### Duplicates

Re-importing overlapping exports is safe and idempotent. Three layers:

1. **Exact file** — content hash matches a previous successful import, so the
   whole file is skipped (`duplicate_file`).
2. **Source record id** — `(source, source_record_id)` is unique. This wins
   over content, so an export that repeats an entry with an edited label is
   still recognised as the same entry.
3. **Content fingerprint** — for records with no id: a digest of the normalized
   instant, emotion fields, affect values, sorted tags, and a **hash** of the
   note. Raw note text is never an input to the digest, so the fingerprint is
   safe to store, log, and return.

### Notes

Notes are off at two independent levels, and both must be opened:

- `privacy.import_notes` — whether note text is read off disk *at all*. Off by
  default, so notes never enter the database and therefore cannot leak later.
- `privacy.expose_notes` — whether a stored note may ever appear in a response.

A client asking for `include_notes: true` when the server says no gets a policy
error. It does not get a quietly note-free result, and it cannot override the
server.

---

## MCP tools

| Tool | What it does | Default |
|---|---|---|
| `mood.get_import_status` | Recent import batches: counts, status, sanitized filename | ✅ available |
| `mood.import_file` | Validate (default) or import one file from the import root | ✅ available, dry run by default |
| `mood.search_entries` | Individual entries in a bounded range | ❌ **policy error by default** |
| `mood.summarize_period` | Aggregate counts and averages per time bucket | ✅ available |
| `mood.detect_low_energy_patterns` | Where low-energy check-ins cluster, with sample size | ✅ available |

Tool names use dots by default. Some MCP clients restrict tool names to
`[A-Za-z0-9_-]`; set `tool_name_style: underscore` for `mood_search_entries`.
Both spellings are accepted on the wire regardless.

`mood.import_file` takes a path **relative to the configured import root**.
Absolute paths, drive letters, UNC prefixes, `..`, and symlinks resolving
outside the root are all rejected, and containment is re-checked after symlink
resolution.

Ranges wider than `max_query_days_without_confirmation` (90 days) require
`confirm_extended_range: true`, so a model cannot sweep your whole history in
one unremarkable-looking call.

### Administrative commands (CLI only, never MCP)

Destructive operations are deliberately outside the MCP surface — nothing a
model says can delete your data.

```bash
lotus-mcp files                 # what's waiting in incoming/
lotus-mcp mappings              # show configured mappings
lotus-mcp batches --limit 20
lotus-mcp rollback <batch-id>   # delete every entry from one batch
lotus-mcp redact-notes          # strip all note text, keep dedup working
lotus-mcp wipe                  # interactive confirmation required
lotus-mcp rebuild               # re-import everything in processed/
```

---

## Docker

```bash
cp .env.example .env      # edit LOTUS_HOST_DIR, LOTUS_UID, LOTUS_GID
docker compose config     # validate
docker compose up -d      # idle maintenance container
```

The image runs as a non-root user, with a read-only container filesystem, all
capabilities dropped, and `no-new-privileges`. Writable mounts are limited to
`data/`, `imports/` and `logs/`; `config/` and `secrets/` are mounted read-only,
so the process cannot rewrite the consent settings that constrain it.

**There are no published ports.** Lotus speaks stdio, so there is no socket to
expose. The default deployment is not reachable from the network.

Run admin commands against the running container:

```bash
docker compose exec lotus-mcp lotus-mcp validate moods.csv --source-type manual_csv
docker compose exec lotus-mcp lotus-mcp import   moods.csv --source-type manual_csv
```

### ZimaOS / NAS layout

Host paths are configurable — nothing is hardcoded. ZimaOS typically uses
`/DATA/AppData`; Unraid uses `/mnt/user/appdata`; Synology uses
`/volume1/docker`. Set whichever is real for your box in `.env`:

```
LOTUS_HOST_DIR=/DATA/AppData/lotus-mcp
LOTUS_UID=1000
LOTUS_GID=1000
```

```
/DATA/AppData/lotus-mcp/
  config/            # config.yaml           (read-only in container)
  secrets/           # unused today          (read-only in container)
  imports/
    incoming/        # drop files here
    processed/       # successful imports land here
    failed/          # rejected files land here, unmodified
  data/              # mood.db lives here
  logs/
```

Ownership must match `LOTUS_UID:LOTUS_GID` or the container cannot write:

```bash
sudo mkdir -p /DATA/AppData/lotus-mcp/{config,secrets,data,logs,imports/{incoming,processed,failed}}
sudo chown -R 1000:1000 /DATA/AppData/lotus-mcp
sudo chmod -R 750 /DATA/AppData/lotus-mcp
cp config/config.example.yaml /DATA/AppData/lotus-mcp/config/config.yaml
```

For remote access to the NAS itself, use Tailscale, WireGuard, or a private
Docker network. Do not port-forward the NAS. Lotus itself exposes nothing.

---

## Registering with Odysseus

When Lotus is bundled in the Odysseus repository, it is auto-registered as
`Built-in: Lotus`; do not add a second custom MCP entry. Rebuild and recreate
Odysseus after adding the package:

```bash
docker compose up -d --build --force-recreate odysseus
```

The existing Odysseus `/app/data` volume persists everything under:

```text
data/lotus/config.yaml
data/lotus/data/mood.db
data/lotus/imports/incoming/
data/lotus/imports/processed/
data/lotus/imports/failed/
```

No token, Docker socket, port, or Lotus sidecar is required. The server starts
with conservative built-in policy defaults when `config.yaml` is absent. To
review or change those defaults, copy `config/config.example.yaml` to
`data/lotus/config.yaml`, then restart Odysseus. Compose's `LOTUS_DATA_DIR` and
`LOTUS_IMPORT_ROOT` settings keep its database and imports in the paths above.

For a standalone Lotus checkout that is not bundled into Odysseus, register it
as a custom MCP server with one of the following alternatives.

**Local install**

```json
{
  "name": "lotus",
  "command": "lotus-mcp",
  "args": ["serve"],
  "env": { "LOTUS_CONFIG": "/absolute/path/to/config/config.yaml" }
}
```

**Or via the module, without installing the console script**

```json
{
  "name": "lotus",
  "command": "python",
  "args": ["-m", "lotus_mcp.server"],
  "env": { "PYTHONPATH": "/path/to/lotus-mcp/src",
           "LOTUS_CONFIG": "/path/to/config/config.yaml" }
}
```

**Containerised**

```json
{
  "name": "lotus",
  "command": "docker",
  "args": ["compose", "-f", "/DATA/AppData/lotus-mcp/compose.yaml",
           "run", "--rm", "-T", "lotus-mcp", "lotus-mcp", "serve"]
}
```

`-T` disables TTY allocation, which stdio transport requires.

---

## Example Odysseus workflows

These are supportive planning aids. None of them is a clinical tool, and Lotus
will not produce a diagnosis for any of them.

**Executive-dysfunction morning triage.** Ask for
`mood.detect_low_energy_patterns` with `group_by: hour_of_day` over the last 30
days, then have Odysseus schedule the hardest task in whichever block has the
*fewest* low-energy check-ins. The tool reports sample size; if it says the
data is thin, take the suggestion as a coin flip rather than a finding.

**Minimum viable low-energy day.** On a morning that already feels rough, ask
for `mood.summarize_period` over the past two weeks grouped by `day_of_week`.
Use it to pick a realistic three-item list instead of yesterday's ten-item one.

**Weekly mood-aware planning.** `mood.summarize_period` with `group_by: week`
over the last 8–12 weeks, alongside your calendar, to notice which weeks were
overloaded — as context for what you commit to next, not as a verdict on them.

**Import-staleness explanation.** `mood.get_import_status` answers "why does
Odysseus think I've been fine?" — usually because the last import was three
weeks ago. Ask for it whenever a summary feels wrong.

---

## Backup and restore

Everything lives under `LOTUS_HOST_DIR`. Stop the container, copy the whole
tree, restart:

```bash
docker compose stop
tar czf lotus-backup-$(date +%F).tar.gz -C /DATA/AppData lotus-mcp
docker compose start
```

Stop first: copying a live SQLite database in WAL mode can produce a torn
snapshot. Restore by putting the tree back and starting the container.

**A backup of this data is as sensitive as the data.** See PRIVACY.md.

`imports/processed/` is the record of what you approved, so the database can be
rebuilt from it:

```bash
lotus-mcp rebuild --fresh
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Raw mood entries are not exposed by this server` | Working as designed | Use `mood.summarize_period`, or set `privacy.expose_raw_entries: true` |
| `Journal notes are not exposed by this server` | Working as designed | Leave it; or open both `import_notes` and `expose_notes` |
| `timestamp has no timezone…` | Naive timestamps, no declared zone | Add `assume_timezone` to a mapping |
| `ambiguous local time` | Timestamp inside a DST fall-back hour | Store that row with an explicit offset |
| `No timestamp column found` | Your header doesn't match any alias | Add the column name to a mapping's `occurred_at` aliases |
| `Duplicate column name(s)` | Two columns fold to the same name | Rename one; Lotus won't guess which is the note |
| `No verified parser exists for source_type` | Health-export types have no parser | Convert to generic CSV/JSON, import as `manual_csv` |
| `Absolute paths are not accepted` | Path escaped the import root | Pass a name relative to `imports/incoming/` |
| Container restarts / can't write | Host dir ownership ≠ `LOTUS_UID:GID` | `chown -R` the host directory |
| `duplicate_file` on a file you edited | Content hash matched a past import | Only true if the bytes are identical; check you saved |

---

## Limitations

- **The database is not encrypted by this application.** It is ordinary SQLite.
  Put it on an encrypted volume. See PRIVACY.md — mounting a Docker volume is
  not encryption and Lotus never claims otherwise.
- No vendor parser for any mood app. Only generic CSV/JSON.
- Apple Health and Health Connect are accepted as labels but refused at import;
  there is no parser and Lotus will not guess at one.
- Note-theme extraction is an interface, not an implementation. When permitted,
  it honestly reports `not_implemented` rather than reaching for an external
  model.
- stdio transport only. No HTTP transport ships — see SECURITY.md for why a
  half-secured listener was left out rather than built.
- Single user. There is no per-user isolation in the schema yet.
- Summaries describe when you *chose to log*, which is not the same as how you
  were. Every summary says so.

---

## License

MIT. See PRIVACY.md and SECURITY.md before deploying.

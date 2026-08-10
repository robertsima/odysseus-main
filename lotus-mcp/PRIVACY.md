# Privacy

Mood check-ins and journal notes are among the most sensitive things a person
keeps a record of. They describe internal state, they often name other people,
and they are the kind of data that is damaging in proportion to how personal it
is. Lotus is built on the assumption that this data should stay on your
hardware, be exposed in the smallest useful form, and be deletable.

## What Lotus does with your data

- Reads CSV/JSON files **you** place in an approved local directory.
- Stores normalized records in a local SQLite database.
- Answers a small set of read-only questions over that database.

## What Lotus never does

- **No outbound network calls.** Not telemetry, not analytics, not model calls.
  This is not merely a promise: with `allow_external_network_calls: false`
  (the default) the process installs a socket guard that refuses any connection
  to a non-loopback address.
- **No writeback.** Lotus never modifies any external service, and no write
  interface is implemented.
- **No cross-domain correlation.** Mood data is never joined to your calendar,
  sleep, email, location, or activity data. That gate is closed by default and
  there is no connector behind it.
- **No clinical claims.** Lotus counts check-ins. It does not diagnose, screen,
  or assess. Every summary carries that statement, and the low-energy tool
  reports observations ("the imported entries contain N low-energy check-ins on
  Monday") rather than conclusions about you.
- **No automatic AI analysis of your journal notes.** Notes are not summarized,
  embedded, classified, or sent anywhere.

## Default consent boundaries

Shipped defaults, enforced in the server rather than requested of the client:

| Setting | Default | Effect |
|---|---|---|
| `expose_aggregate_summaries` | `true` | Counts and averages are available |
| `expose_emotion_labels` | `true` | Labels appear in summaries |
| `expose_raw_entries` | **`false`** | Individual records are not returned |
| `expose_notes` | **`false`** | Note text never appears in a response |
| `include_note_themes` | **`false`** | No themes derived from notes |
| `import_notes` | **`false`** | Note text is not even read off disk |
| `allow_cross_domain_correlation` | **`false`** | No joins to other data |
| `allow_writeback_to_source` | **`false`** | No writes anywhere |
| `allow_external_network_calls` | **`false`** | Socket guard active |
| `max_query_days_without_confirmation` | `90` | Wider ranges need explicit confirmation |
| `maximum_entry_result_limit` | `500` | Hard ceiling on rows per search |

A client cannot widen any of these. `include_notes: true` from a model is
combined with the server setting using AND, and an explicit request for
something the policy forbids returns a policy error rather than silently
returning less — so the refusal is visible instead of looking like an empty
result.

The active policy is written to the `consent_policies` table when the server
starts, so the boundary that data was collected under is auditable later.

## Notes are off twice, on purpose

`import_notes` and `expose_notes` are separate settings because they fail
differently:

- With `import_notes: false`, note text never enters the database. There is no
  stored note to leak through a future bug, a backup, or a misconfiguration.
- With `expose_notes: false` but notes stored, the note *column is not named in
  the SELECT at all* — there is no code path where note text is loaded and then
  relied upon to be stripped later.

If you enable note import and change your mind, `lotus-mcp redact-notes`
removes every stored note while keeping the note hashes, so deduplication keeps
working and re-importing the same export will not create duplicates.

Notes are never written to logs, never included in error messages, never part
of a fingerprint, and never echoed in an import report — rejection reasons name
the field and the limit, never the value.

## Encryption — read this before you trust it

**Lotus does not encrypt its database.** `data/mood.db` is ordinary SQLite.
Anyone who can read that file can read every mood record and every stored note.

Specifically:

- Mounting a directory into a Docker container is **not** encryption. It is a
  path. Lotus will never describe it as anything else.
- SQLCipher is not implemented in this version. When the health command reports
  encryption status it says `none (plain SQLite; use an encrypted host volume)`,
  because claiming otherwise would be worse than not encrypting at all.

What to do instead:

1. Put `LOTUS_HOST_DIR` on an encrypted volume (LUKS, ZFS native encryption,
   an encrypted NAS share, FileVault, BitLocker).
2. Or leave `import_notes: false`, which keeps the most sensitive field out of
   the database entirely. The remaining rows — a timestamp and a label — are
   still personal, but far less exposing than journal text.

Application-layer note encryption is a designed extension point. It is not
built, and this document will say so until it is, tested.

## Retention and deletion

Lotus never expires data on its own; you decide.

```bash
lotus-mcp rollback <batch-id>   # remove one import's entries
lotus-mcp redact-notes          # remove all note text, keep the rest
lotus-mcp wipe                  # delete everything (interactive confirmation)
```

`wipe` requires typing a confirmation phrase at a terminal. It is not reachable
over MCP, so no model can trigger it.

Deleting the database does not delete the source files in `imports/processed/`.
Delete those separately if you want the data gone — and remember the originals
are wherever you first exported them.

## Backup risk

A backup of Lotus is a complete copy of your mood history. It inherits none of
the protections of the machine it came from:

- Do not put it in general-purpose cloud storage unless it is encrypted first.
- A backup taken while notes were enabled still contains those notes, even
  after you run `redact-notes` on the live database.
- Backups made before you tightened a privacy setting reflect the *old*
  boundary. The `consent_policies` table records which boundary applied.

## Sharing outputs

Aggregate summaries are much safer to share than raw entries, but they are not
anonymous — "14 low-energy check-ins in the week of March 3" says a great deal
about a particular person's March. Treat any Lotus output as personal data.

## Reporting a privacy concern

See SECURITY.md.

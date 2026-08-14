# Markdown vault retrieval

How Odysseus indexes and retrieves an Obsidian-style `.md` knowledge base, and
which knobs change it.

Nothing here needs configuring to work. The defaults are the intended
behaviour; the environment variables exist for vaults whose shape differs from
the assumptions.

## What gets extracted from a note

Indexing a `.md` or `.markdown` file parses it as Markdown rather than as a
wall of text (`src/vault_markdown.py`):

| Source | Becomes |
|---|---|
| YAML frontmatter `tags` / `aliases` | searchable metadata, matched by tag and alias queries |
| Frontmatter `updated` / `modified` / `date` / `created` | the note's date, used for temporal ranking |
| A date in the filename (`2026-08-14.md`, `20260814.md`) | the note's date, when frontmatter has none |
| File mtime | the note's date, as a last resort — and discounted, because a re-sync moves it |
| `#inline-tags` in the body | additional tags (code fences and headings excluded) |
| `[[wikilinks]]` | the vault's link graph, used for multi-source retrieval |
| Headings | a breadcrumb (`Odysseus > Deployment > NAS`) on every chunk |

Frontmatter is removed from the indexed prose — it used to be embedded as
`---`/`tags:` noise in each note's first chunk.

## How a note is chunked

Along heading boundaries, not sentence boundaries. Sections are packed together
until the next one would exceed the chunk size, so a note made of one-line
headings does not explode into dozens of near-empty chunks; a section larger
than the chunk size falls back to the existing sentence-aware splitter.

Every chunk is prefixed with a compact provenance header before it is embedded:

```
Source: Vault Mind.md Vault Mind
Section: Retrieval > Ranking
Tags: #project #ai
Updated: 2026-08-14
```

That header is in the chunk **text** deliberately. Retrieval scores against
text — the embedding is computed from it, and the keyword half of the hybrid
score tokenises it — so a tag or date held only in metadata is unreachable by
any query. It is stripped again before the chunk is shown to the model, which
sees the same facts once, in the snippet label.

## How results are ranked

On top of the existing hybrid score (0.7 × vector similarity + 0.3 × keyword
overlap) and the filename-match floor:

**Tags and aliases.** `#project` in a query names a set deliberately. Prose
overlap barely registers a tag that appears once in a frontmatter block, so a
tag or alias match takes its own credit instead. Nested tags match on any
segment — `#project` finds `#project/odysseus`.

**Recency.** A knowledge base accumulates, and the note that recorded a
decision in 2024 is just as *similar* to "what's our current deploy process" as
the one that replaced it. Rank decays exponentially with the note's age. By
default this is a tie-break worth a few percent — an old note is not a wrong
note. When the query is explicitly about the present ("current", "latest",
"has this changed", "still true"), it counts six times as much, which is what
stops a superseded note answering as though it were still policy.

Recency never lets a chunk cross a rank class: a document the query names by
name still outranks every document it did not, however old.

**Link expansion.** After the primary passes, the `[[wikilinks]]` of the top
few results are followed and those notes are retrieved too, at a discount. This
is the case ordinary similarity search cannot reach: the question is about the
party, and the constraint that makes the answer correct ("Sarah is allergic to
peanuts") lives in a note sharing no vocabulary with it — but the vault already
records the connection. Linked results are marked `via: link` in the sources
list so they read as supporting context, not as direct hits.

**Diversity.** At most two chunks from any one file, unless there is nothing
else to return. Five chunks of one note is five copies of one source; it
crowds out the second opinion that would have revealed a conflict, and it is
the largest avoidable cost in a retrieval block.

## What the model receives

Each snippet is labelled with its file, date, section and tags, and the block
opens with an instruction to treat the more recently updated document as
current when two disagree — and to say which it used. Dates without that
instruction are decoration; the model reads two contradictory notes as one
contradictory corpus and picks arbitrarily.

The character budget is shared evenly across sources rather than spent
front-to-back. Previously a single long note could consume the whole budget and
silently truncate every source after it out of the prompt.

## Keeping the index current

`src/vault_scan.py` polls tracked directories every 30s (`ODYSSEUS_VAULT_SCAN_SECONDS`)
and re-indexes only files whose `(mtime, size)` changed. Vault files mounted
read-only from elsewhere are handled the same way — polling is used precisely
because inotify events do not cross a Docker bind mount.

`STATE_VERSION` in that module is bumped whenever the chunk *format* changes.
A bump discards the scan state, so the next scan treats every tracked file as
changed and rewrites it once. Without that an already-indexed vault would keep
its old chunks forever, because every file still looks up-to-date.

## Configuration

All optional. See `.env.example` for the same list with defaults inline.

| Variable | Default | Effect |
|---|---|---|
| `RAG_SIMILARITY_THRESHOLD` | `0.35` | Minimum blended score to be injected at all |
| `ODYSSEUS_RAG_RECENCY_HALFLIFE_DAYS` | `180` | How fast rank decays with age |
| `ODYSSEUS_RAG_TEMPORAL_WEIGHT` | `0.05` | Recency weight on an ordinary query; `0` disables |
| `ODYSSEUS_RAG_TEMPORAL_INTENT_WEIGHT` | `0.30` | Recency weight when the query is about the present |
| `ODYSSEUS_RAG_MAX_CHUNKS_PER_DOC` | `2` | Per-file cap; `0` disables |
| `ODYSSEUS_RAG_LINK_EXPANSION` | `1` | Follow `[[wikilinks]]`; `0` disables |
| `ODYSSEUS_VAULT_DATE_ORDER` | `day` | Reading of an ambiguous filename date (`03-04-2026`) |
| `ODYSSEUS_VAULT_SCAN_SECONDS` | `30` | Re-scan interval; `0` disables |

## Backwards compatibility

Every signal degrades to neutral when its metadata is absent, so chunks indexed
before this existed rank exactly as they did. Non-Markdown files are indexed
unchanged. A note that fails to parse as Markdown is indexed as plain text
rather than dropped.

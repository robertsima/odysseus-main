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

The cap doubles when the query named what it wants — a `#tag`, or a file by
name. A flat cap is the wrong shape for both cases at once. On an open question
("what did I decide about storage") breadth is what surfaces the reference note
sitting behind a pile of journal entries. On `#homelab` the user has already
said which notes they mean, and trading their best passages for weaker ones
from notes nobody asked about is a downgrade. It stays a cap either way, so the
conflicting second source still gets in.

## What the model receives

Each snippet is labelled with its file, date, section and tags, and the block
opens with an instruction to treat the more recently updated document as
current when two disagree — and to say which it used. Dates without that
instruction are decoration; the model reads two contradictory notes as one
contradictory corpus and picks arbitrarily.

The character budget is shared evenly across sources rather than spent
front-to-back. Previously a single long note could consume the whole budget and
silently truncate every source after it out of the prompt.

## Reaching a note as a file

Retrieval and file access are two views of one tree, and they are meant to be
used together: `search_documents` finds the passage and returns the **real
path** of the file it came from, and that path goes straight into `read_file`
(with `offset`/`limit`), `edit_file`, or `write_file`.

Three rules make that hold:

**One path per note.** The vault is mounted once, under
`/app/data/personal_docs/…`. It used to be mounted twice — read-only there for
indexing and again under `/app/workspace/…` for the file tools — so the path
retrieval cited could be read but not written, while the writable copy sat at a
second path that was off the tool allowlist and reachable only by binding a
workspace. Mount each tree once, and make it `rw` if the agent should be able
to edit it.

**The knowledge base is reachable with a workspace bound.** Binding a workspace
(`/workspace set`) confines the file tools to that folder — plus the
personal-documents tree, which stays in reach either way. Without that carve-out
a coding turn could not consult the vault, and editing the vault meant binding
the vault as the workspace, which revoked everything else.

**The `private` label is the thing that closes a directory**, not the `:ro`
mount. `_resolve_tool_path` rejects any path under a directory labelled private,
so a private Journal is unreadable by `read_file` however it is mounted.
Declare labels in the compose file with `ODYSSEUS_PERSONAL_DIRS`
(`Vault Mind:public,AI Mind:public,Journal:private`) — an *undeclared* directory
is a **public** directory, and on a fresh install the labels do not exist until
something writes them.

Directories outside `/app/data` that the file tools should reach are declared
with `ODYSSEUS_TOOL_EXTRA_ROOTS` (path-separator or comma separated). The
sensitive-name deny list (`.ssh`, `.gnupg`, `id_rsa`, …) applies inside every
root, extra roots included.

### Why the agent used to grep instead

Tool selection is per-turn. Naming the vault marks the turn as file work, which
swaps in the file/shell toolset — and the system-prompt rule packs are derived
from the *selected tool names*, so a turn with no `search_documents` also had no
rule telling it to search. What it did have was the file pack: "prefer `grep`,
`glob` and `ls`." `search_documents` is now seeded whenever the request names
the knowledge base, after that swap, and it carries the **Knowledge base rules**
pack with it: search first, the returned paths are directly readable and
writable, and no workspace needs to be set.

## Embeddings and the vector store

These are two layers and they are often confused for alternatives:

| | What it is | Configured by |
|---|---|---|
| **ChromaDB** | the vector **store**, an HTTP service (`:8100`) that holds the chunks and their vectors | `CHROMADB_HOST` / `CHROMADB_PORT` |
| **Embedder** | what turns text into those vectors | an HTTP endpoint (`EMBEDDING_URL`, or the admin panel), else local FastEmbed |

Turning one off does not select the other. With Chroma unreachable there is no
retrieval at all — `search_documents` says so explicitly, `get_rag_manager()`
returns `None` and throttles its retries to once per 30s.

Vectors from different models cannot share a collection (Chroma fixes a
collection's dimension on first insert), so each embedder gets its own **lane**
and its own collection: `custom` for the HTTP endpoint, `fastembed` for the
local fallback. Both are searched, best-scoring lane wins.

`ODYSSEUS_FASTEMBED_LANE` controls whether the fallback lane is maintained:

- `auto` (default) — always build it, so retrieval survives the endpoint going
  down.
- `off` — build it only when the custom lane failed to come up. Use this when
  you run a real embedding model and do not want a second 384-dimension MiniLM
  index maintained beside it. It still falls back rather than leaving the app
  with no lanes: an unreachable endpoint must degrade retrieval, not delete it.

Switching embedders re-embeds rather than corrupting: a lane whose fingerprint
(model + url + dimension) no longer matches its collection is rebuilt from the
stored documents.

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
| `ODYSSEUS_RAG_TAG_CREDIT` | `1.0` | Scales tag/alias credit; `0` disables |
| `ODYSSEUS_RAG_MAX_CHUNKS_PER_DOC` | `2` | Per-file cap, doubled when the query names a tag or file; `0` disables |
| `ODYSSEUS_RAG_FOCUSED_CAP_MULTIPLIER` | `2` | How far that cap relaxes for such a query; `1` disables the relaxation only |
| `ODYSSEUS_RAG_LINK_EXPANSION` | `1` | Follow `[[wikilinks]]`; `0` disables |
| `ODYSSEUS_VAULT_DATE_ORDER` | `day` | Reading of an ambiguous filename date (`03-04-2026`) |
| `ODYSSEUS_VAULT_SCAN_SECONDS` | `30` | Re-scan interval; `0` disables |
| `ODYSSEUS_FASTEMBED_LANE` | `auto` | `off` skips the local fallback lane when an embedding endpoint is up |
| `ODYSSEUS_TOOL_EXTRA_ROOTS` | *(empty)* | Extra directories the agent's file tools may touch |
| `ODYSSEUS_PERSONAL_DIRS` | *(empty)* | `path:label` pairs to index at boot; an undeclared directory is public |

## Measuring it on a live index

Every ranking signal is applied at query time and has an off-switch, so both
configurations can be run against the same index with no re-index between them.
`scripts/rag_ab_compare.py` does exactly that. Run it where ChromaDB is
reachable — inside the app container, not on the host:

```sh
# What signals do your notes actually carry?
docker compose exec odysseus python scripts/rag_ab_compare.py --coverage

# Does the new ranking change these queries?
docker compose exec odysseus python scripts/rag_ab_compare.py \
    "what is my current approach to X" "#project notes" "what changed recently"
```

`--coverage` is the one to run first. It reads the metadata off every stored
chunk and reports how many notes have dates (and whether those come from
frontmatter, filenames or mtime), tags, aliases, wikilinks and heading paths.
A signal with no metadata behind it cannot change anything, so this is what
separates "the feature isn't working" from "your vault doesn't express that."

It also confirms the one-time re-index ran: a chunk written by the old indexer
has no `note_key`.

The query mode runs each query twice — new signals off, then on — and diffs
the two result lists. Identical output is a legitimate result: on a query where
relevance alone was already right, nothing *should* move.

The one thing it cannot A/B is the chunk format (heading-aware splitting, the
provenance header), because that is fixed at index time. `--coverage` is the
proxy for that half.

## Backwards compatibility

Every signal degrades to neutral when its metadata is absent, so chunks indexed
before this existed rank exactly as they did. Non-Markdown files are indexed
unchanged. A note that fails to parse as Markdown is indexed as plain text
rather than dropped.

"""
tool_output_store.py

Overflow store for oversized tool results.

A tool result goes into the transcript once and is then replayed on every
later round of the same turn, so one `cat` of a 60k-character log is not a
60k-character cost — it is 60k times the number of rounds that follow it. The
agent loop already caps what the UI renders, but the model-facing text built by
`format_tool_result` had no ceiling at all, which is how a single agent turn
was observed at 69k tokens with 62% of it whole-file tool output.

So: keep the head and tail of a large result inline (that is where the shape of
the output, the command echo, and the error line almost always are), write the
full text to disk, index it in the same ChromaDB/embedding stack the rest of
the app uses, and hand the model a reference it can search with
`recall_tool_output`. Nothing is lost — it just stops riding along in context
until it is actually wanted.

Disk is the source of truth; the vector index is an accelerator. When ChromaDB
is unreachable the store still works: exact slices are served from the file and
`search` degrades to a keyword scan, so a broken embedding backend costs
retrieval quality, never the data.
"""

import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

COLLECTION_NAME = "odysseus_tool_outputs"

# Results at or below this stay inline verbatim. ~4k chars is roughly 1k
# tokens: big enough that ordinary command output, directory listings and API
# responses are untouched, small enough that a log dump or a whole-file read
# gets offloaded.
DEFAULT_INLINE_LIMIT = 4000
# What stays in context when a result IS offloaded.
DEFAULT_HEAD_CHARS = 2000
DEFAULT_TAIL_CHARS = 800

# Tools whose result the agent needs verbatim get more room before an excerpt
# replaces it. `edit_file` matches an exact string against what `read_file`
# returned, so trimming the middle out of a source file at 4k would turn a
# working edit into a failed one. 20k is `MAX_READ_CHARS`: read_file never
# returns more than that anyway, so in practice only a full-size read (or a
# read plus a long header) offloads, and the excerpt then tells the agent to
# page through with an offset — which read_file itself already supports.
_TOOL_INLINE_LIMITS = {
    "read_file": 20_000,
    "apply_patch": 20_000,
    "edit_file": 20_000,
}

_CHUNK_CHARS = 1200
_CHUNK_OVERLAP = 120
_MAX_CHUNKS = 200  # ~240k chars indexed per output; the file keeps the rest
_RETENTION_DAYS = 7

_REF_RE = re.compile(r"\btoolout-[0-9a-f]{10}\b")


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def inline_limit(tool: str = "") -> int:
    """How much of this tool's result may stay inline before it is offloaded."""
    base = _env_int("ODYSSEUS_TOOL_OUTPUT_INLINE_LIMIT", DEFAULT_INLINE_LIMIT)
    return max(base, _TOOL_INLINE_LIMITS.get(str(tool or ""), 0))


def head_chars() -> int:
    return _env_int("ODYSSEUS_TOOL_OUTPUT_HEAD_CHARS", DEFAULT_HEAD_CHARS)


def tail_chars() -> int:
    return _env_int("ODYSSEUS_TOOL_OUTPUT_TAIL_CHARS", DEFAULT_TAIL_CHARS)


def _store_dir() -> str:
    from src.constants import DATA_DIR

    path = os.path.join(DATA_DIR, "tool_outputs")
    os.makedirs(path, exist_ok=True)
    return path


def _paths(ref: str) -> tuple:
    base = os.path.join(_store_dir(), ref)
    return base + ".txt", base + ".json"


def is_ref(value: Any) -> bool:
    return bool(value) and bool(_REF_RE.fullmatch(str(value).strip()))


def _new_ref(text: str, tool: str) -> str:
    digest = hashlib.sha1(
        f"{tool}|{time.time_ns()}|{len(text)}|{text[:256]}".encode("utf-8", "replace")
    ).hexdigest()
    return f"toolout-{digest[:10]}"


def _chunk(text: str) -> List[str]:
    """Split on line boundaries into overlapping windows.

    Line-aligned so a chunk that matches a query can be read back as something
    a human (or the model) recognises, rather than starting mid-token.
    """
    chunks: List[str] = []
    start = 0
    length = len(text)
    while start < length and len(chunks) < _MAX_CHUNKS:
        end = min(start + _CHUNK_CHARS, length)
        if end < length:
            newline = text.rfind("\n", start + _CHUNK_CHARS // 2, end)
            if newline > start:
                end = newline
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        start = max(end - _CHUNK_OVERLAP, start + 1)
    return chunks


def _prune(now: Optional[float] = None) -> None:
    """Drop stored outputs older than the retention window.

    Best effort and cheap: this is scratch data whose only consumer is the
    agent turn that produced it (plus a later turn in the same chat that says
    "go back to that log"). Nothing outside this module depends on it existing.
    """
    now = now or time.time()
    cutoff = now - _RETENTION_DAYS * 86400
    try:
        directory = _store_dir()
        for name in os.listdir(directory):
            if not name.startswith("toolout-"):
                continue
            path = os.path.join(directory, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                continue
    except OSError as exc:  # pragma: no cover - defensive
        logger.debug("tool output prune skipped: %s", exc)


_lane_cache: List[Any] = []
_lane_failed_at = 0.0
_LANE_RETRY_SECONDS = 60.0


def reset_lane_cache() -> None:
    """Forget the cached lanes (config change, or a test swapping the backend)."""
    global _lane_cache, _lane_failed_at
    _lane_cache = []
    _lane_failed_at = 0.0


def _lanes() -> List[Any]:
    """Embedding lanes for this collection, cached, with a failure cooldown.

    Building lanes reaches for ChromaDB, and an unreachable ChromaDB costs a
    connect probe per attempt. Offloading happens on the hot path of an agent
    round, so a down vector store must not add that probe to every large tool
    result — fail once, then stay quiet for a minute.
    """
    global _lane_cache, _lane_failed_at
    if _lane_cache:
        return _lane_cache
    if time.time() - _lane_failed_at < _LANE_RETRY_SECONDS:
        return []
    try:
        from src.embedding_lanes import build_embedding_lanes

        lanes = build_embedding_lanes(COLLECTION_NAME)
    except Exception as exc:
        logger.info("tool output index unavailable (%s); disk-only for now", exc)
        _lane_failed_at = time.time()
        return []
    if not lanes:
        _lane_failed_at = time.time()
        return []
    _lane_cache = list(lanes)
    return _lane_cache


def _index(ref: str, text: str, meta: Dict[str, Any]) -> int:
    """Embed the stored output. Returns the number of indexed chunks."""
    lanes = _lanes()
    if not lanes:
        return 0

    chunks = _chunk(text)
    if not chunks:
        return 0
    ids = [f"{ref}:{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "ref": ref,
            "chunk_index": i,
            "tool": str(meta.get("tool") or ""),
            "session_id": str(meta.get("session_id") or ""),
            "command": str(meta.get("command") or "")[:200],
            "created_at": float(meta.get("created_at") or time.time()),
        }
        for i in range(len(chunks))
    ]
    indexed = 0
    for lane in lanes:
        try:
            lane.collection.add(
                ids=ids,
                embeddings=lane.encode(chunks),
                documents=chunks,
                metadatas=metadatas,
            )
            indexed = len(chunks)
        except Exception as exc:
            logger.warning("tool output %s add failed in %s lane: %s", ref, lane.name, exc)
    return indexed


def store(
    text: str,
    *,
    tool: str = "",
    command: str = "",
    session_id: Optional[str] = None,
    round_num: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Persist a full tool result and index it. Returns its record, or None.

    Never raises: an offload that fails must leave the caller free to keep the
    output inline rather than lose it.
    """
    text = text if isinstance(text, str) else str(text or "")
    if not text.strip():
        return None
    ref = _new_ref(text, tool)
    record = {
        "ref": ref,
        "tool": tool,
        "command": (command or "")[:400],
        "session_id": session_id or "",
        "round": round_num,
        "chars": len(text),
        "lines": text.count("\n") + 1,
        "created_at": time.time(),
    }
    txt_path, meta_path = _paths(ref)
    try:
        with open(txt_path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(text)
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
    except OSError as exc:
        logger.warning("tool output %s could not be written: %s", ref, exc)
        return None

    # Embedding a long output is CPU work and the caller is mid-round inside
    # the event loop, so hand it to a thread when there is one to hand it to.
    # The excerpt the model gets back does not depend on the index: exact
    # slices come off disk, and a query that arrives before indexing finishes
    # falls back to the keyword scan.
    record["indexed_chunks"] = None
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.run_in_executor(None, _index, ref, text, dict(record))
    else:
        record["indexed_chunks"] = _index(ref, text, record)
    _prune()
    logger.info(
        "[tool-output] offloaded %s chars from %s as %s (indexing=%s)",
        record["chars"], tool or "tool", ref,
        "background" if loop is not None else record["indexed_chunks"],
    )
    return record


def load(ref: str) -> Optional[str]:
    """Return the full stored text for a ref, or None if it is gone."""
    if not is_ref(ref):
        return None
    txt_path, _ = _paths(str(ref).strip())
    try:
        with open(txt_path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def load_record(ref: str) -> Optional[Dict[str, Any]]:
    if not is_ref(ref):
        return None
    _, meta_path = _paths(str(ref).strip())
    try:
        with open(meta_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def list_recent(session_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """Stored outputs, newest first, optionally scoped to one chat."""
    out: List[Dict[str, Any]] = []
    try:
        directory = _store_dir()
        names = [n for n in os.listdir(directory) if n.endswith(".json")]
    except OSError:
        return out
    for name in names:
        record = load_record(name[: -len(".json")])
        if not record:
            continue
        if session_id and record.get("session_id") and record["session_id"] != session_id:
            continue
        out.append(record)
    out.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return out[: max(1, limit)]


def _keyword_scan(query: str, text: str, ref: str, k: int) -> List[Dict[str, Any]]:
    """Fallback retrieval when the vector index is unavailable or empty.

    Scores each chunk by how many distinct query terms it contains. Crude on
    purpose — its whole job is to be better than handing back nothing when
    ChromaDB is down.
    """
    terms = {t for t in re.findall(r"\w+", query.lower()) if len(t) > 2}
    if not terms:
        return []
    hits = []
    for i, chunk in enumerate(_chunk(text)):
        lowered = chunk.lower()
        matched = sum(1 for t in terms if t in lowered)
        if matched:
            hits.append({
                "ref": ref,
                "chunk_index": i,
                "text": chunk,
                "score": round(matched / len(terms), 4),
                "match": "keyword",
            })
    hits.sort(key=lambda h: (-h["score"], h["chunk_index"]))
    return hits[:k]


def search(
    query: str,
    *,
    ref: Optional[str] = None,
    session_id: Optional[str] = None,
    k: int = 5,
) -> List[Dict[str, Any]]:
    """Semantic search over stored tool outputs.

    Scoped to one output when `ref` is given, otherwise to the chat's outputs.
    """
    query = (query or "").strip()
    if not query:
        return []
    k = max(1, min(int(k or 5), 20))

    where: Optional[Dict[str, Any]] = None
    if ref and is_ref(ref):
        where = {"ref": str(ref).strip()}
    elif session_id:
        where = {"session_id": str(session_id)}

    rows: List[Dict[str, Any]] = []
    try:
        from src.embedding_lanes import build_embedding_lanes, dedupe_results, query_lanes

        lanes = build_embedding_lanes(COLLECTION_NAME)
        results = query_lanes(
            lanes,
            query,
            n_results=lambda lane: k,
            include=["documents", "metadatas", "distances"],
            where=where,
        )
        for _lane, res in results:
            ids = (res.get("ids") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            for i, chunk_id in enumerate(ids):
                meta = metas[i] if i < len(metas) else {}
                rows.append({
                    "id": chunk_id,
                    "ref": (meta or {}).get("ref") or ref or "",
                    "chunk_index": (meta or {}).get("chunk_index", i),
                    "tool": (meta or {}).get("tool", ""),
                    "text": docs[i] if i < len(docs) else "",
                    "score": round(1.0 - (dists[i] if i < len(dists) else 1.0), 4),
                    "match": "semantic",
                })
        rows.sort(key=lambda r: -r.get("score", 0))
        rows = dedupe_results(rows, id_key="id", limit=k)
    except Exception as exc:
        logger.info("tool output search fell back to keyword scan: %s", exc)
        rows = []

    if rows:
        return rows

    # No vector hits (index down, or this output was never indexed) — scan the
    # files we do have rather than reporting nothing.
    targets = [ref] if (ref and is_ref(ref)) else [
        r["ref"] for r in list_recent(session_id, limit=5)
    ]
    for target in targets:
        text = load(target)
        if text:
            rows.extend(_keyword_scan(query, text, target, k))
    rows.sort(key=lambda r: -r.get("score", 0))
    return rows[:k]


def excerpt_with_pointer(
    text: str,
    record: Dict[str, Any],
    *,
    head: Optional[int] = None,
    tail: Optional[int] = None,
) -> str:
    """The inline stand-in for an offloaded result: head + tail + how to get more."""
    head = head if head is not None else head_chars()
    tail = tail if tail is not None else tail_chars()
    ref = record.get("ref", "")
    hidden = max(len(text) - head - tail, 0)
    parts = [
        text[:head].rstrip(),
        (
            f"\n\n[... {hidden:,} of {record.get('chars', len(text)):,} characters "
            f"held outside the conversation as `{ref}` ...]\n\n"
        ),
        text[-tail:].lstrip() if tail else "",
        (
            f"\n\nThis output was large, so only its head and tail are shown. The full "
            f"{record.get('lines', 0):,}-line result is stored and searchable: call "
            f"`recall_tool_output` with {{\"ref\": \"{ref}\", \"query\": \"<what you need>\"}} "
            f"to pull the relevant part, or {{\"ref\": \"{ref}\", \"offset\": <char>}} to read "
            f"it in order. Do NOT re-run the tool to see the rest."
        ),
    ]
    return "".join(parts)


def maybe_offload(
    formatted: str,
    *,
    tool: str = "",
    command: str = "",
    session_id: Optional[str] = None,
    round_num: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple:
    """Return (text_for_the_model, record_or_None).

    Small results are returned untouched, so the common case costs one length
    check. Anything past the limit is stored and replaced by an excerpt that
    names its ref.
    """
    if not isinstance(formatted, str):
        return formatted, None
    limit = limit if limit is not None else inline_limit(tool)
    if len(formatted) <= limit:
        return formatted, None
    try:
        record = store(
            formatted,
            tool=tool,
            command=command,
            session_id=session_id,
            round_num=round_num,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("tool output offload failed for %s: %s", tool, exc)
        return formatted, None
    if not record:
        return formatted, None
    return excerpt_with_pointer(formatted, record), record

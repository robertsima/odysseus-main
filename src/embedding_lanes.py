"""
embedding_lanes.py

Helpers for the supported local FastEmbed + Chroma retrieval path.

The application deliberately has one embedding implementation: local FastEmbed.
Remote/custom endpoints and legacy unsuffixed collections are not runtime paths.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

LANE_FASTEMBED = "fastembed"
LANE_CUSTOM = "custom"

# Whether the local FastEmbed fallback lane is built alongside a working
# custom (HTTP) embedding endpoint.
#
#   "auto" (default) -- always build it. Both lanes are indexed and both are
#       searched, so retrieval survives the endpoint going away.
#   "off"            -- build it ONLY when the custom lane failed to come up.
#
# "off" exists because "we run a real embedding model, stop also maintaining a
# second 384-dimension MiniLM index" was a reasonable thing to want and there
# was no way to say it: every offload, index and search paid for both lanes,
# and the FastEmbed model was downloaded and loaded on a box that had no use
# for it. It deliberately still falls back rather than leaving the app with no
# lanes at all -- an unreachable endpoint must degrade retrieval, not delete it.
FASTEMBED_LANE_ENV = "ODYSSEUS_FASTEMBED_LANE"


def fastembed_lane_mode() -> str:
    """Normalized value of ODYSSEUS_FASTEMBED_LANE ("auto" or "off")."""
    raw = (os.environ.get(FASTEMBED_LANE_ENV) or "").strip().lower()
    if raw in {"off", "false", "0", "no", "fallback-only", "fallback_only"}:
        return "off"
    return "auto"


# `count()` is a network round-trip to ChromaDB, and the retrieval paths ask
# for it several times per search: once to decide whether the lane is empty,
# again inside query_lanes to clamp n_results, per lane, per store. One chat
# turn was observed making fifteen HTTP calls to Chroma, eleven of them counts,
# to return zero results. A short TTL collapses that burst without letting a
# stale count outlive the request that caused it.
#
# Cached PER LANE INSTANCE, not per collection name. Two lanes can carry the
# same collection name while pointing at different collection objects (a
# rebuilt index, a second client, a test fixture) — a shared cache hands one
# the other's count, which is a wrong answer, not just a stale one.
_COUNT_TTL_SECONDS = 2.0
_count_generation = 0


def invalidate_count_cache(collection_name: Optional[str] = None) -> None:
    """Drop cached counts after a write, so a fresh add is searchable at once.

    `collection_name` is accepted for callers that know what they changed, but
    the counter is global: invalidation is rare and a lane holds no index of
    its peers, so bumping a generation everyone compares against is both
    cheaper and impossible to get subtly wrong.
    """
    global _count_generation
    _count_generation += 1


@dataclass
class EmbeddingLane:
    name: str
    client: Any
    collection: Any
    collection_name: str
    model: str
    url: str
    dimension: int
    fingerprint: str

    @property
    def healthy(self) -> bool:
        return self.collection is not None and self.client is not None

    def encode(self, texts: Sequence[str]) -> List[List[float]]:
        vecs = self.client.encode(list(texts), normalize_embeddings=True)
        return vecs.tolist() if hasattr(vecs, "tolist") else [list(v) for v in vecs]

    def count(self) -> int:
        cached = getattr(self, "_count_cached", None)
        now = time.monotonic()
        if (
            cached is not None
            and cached[0] == _count_generation
            and now - cached[1] < _COUNT_TTL_SECONDS
        ):
            return cached[2]
        try:
            value = int(self.collection.count())
        except Exception:
            return 0
        # Only a NON-EMPTY count is cached. Callers treat 0 as "skip this lane",
        # so a cached zero would hide a document for up to the TTL right after
        # it was added — and writes go through `lane.collection` directly, not
        # through this class, so there is no reliable hook to invalidate on.
        # An empty collection is also the cheap case to re-ask about.
        if value > 0:
            object.__setattr__(self, "_count_cached", (_count_generation, now, value))
        return value

    def stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "collection": self.collection_name,
            "model": self.model,
            "url": self.url,
            "dimension": self.dimension,
            "fingerprint": self.fingerprint,
            "count": self.count(),
            "healthy": self.healthy,
        }


def reset_embedding_lane_state() -> None:
    """Reset process-local embedding lane state after endpoint config changes."""
    global _fastembed_client

    try:
        from src.embeddings import reset_http_embed_state
        reset_http_embed_state()
    except Exception:
        pass
    # The cached fallback client keys off FASTEMBED_MODEL, read once at
    # construction. Drop it here so this stays the one hook that clears every
    # piece of process-local lane state.
    with _fastembed_lock:
        _fastembed_client = None


def collection_name(base_name: str, lane_name: str) -> str:
    return f"{base_name}_{lane_name}"


def _fingerprint(lane_name: str, url: str, model: str, dimension: int) -> str:
    raw = f"{lane_name}\n{url}\n{model}\n{dimension}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _metadata(lane_name: str, url: str, model: str, dimension: int, fingerprint: str) -> Dict[str, Any]:
    return {
        "hnsw:space": "cosine",
        "embedding_lane": lane_name,
        "embedding_url": url,
        "embedding_model": model,
        "embedding_dimension": dimension,
        "embedding_fingerprint": fingerprint,
    }


def _load_custom_endpoint() -> Dict[str, str]:
    try:
        from src.embeddings import _load_persisted_endpoint
        persisted = _load_persisted_endpoint()
    except Exception:
        persisted = {}

    url = persisted.get("url") or os.environ.get("EMBEDDING_URL", "")
    if not url:
        return {}

    model = persisted.get("model") or os.environ.get("EMBEDDING_MODEL", "")
    api_key = persisted.get("api_key") or os.environ.get("EMBEDDING_API_KEY", "")
    if persisted.get("api_key"):
        try:
            from src.secret_storage import decrypt
            api_key = decrypt(api_key)
        except Exception:
            logger.warning("Could not decrypt saved embedding endpoint API key")
            api_key = ""

    return {"url": url, "model": model, "api_key": api_key}


# The FastEmbed fallback client is immutable once built — fixed model name,
# fixed cache dir — but `build_embedding_lanes` was constructing a fresh one on
# every call. That means loading the ONNX model AND running a probe encode for
# the dimension, every time a tool result is offloaded or a stored output is
# searched. One agent turn showed four of those inside eight seconds. Build it
# once per process instead; a failure is not cached, so a transient problem
# (model still downloading) is retried on the next call.
_fastembed_client = None
_fastembed_lock = threading.Lock()


def _build_fastembed_client():
    global _fastembed_client

    if _fastembed_client is not None:
        return _fastembed_client
    with _fastembed_lock:
        if _fastembed_client is not None:
            return _fastembed_client
        from src.embeddings import FastEmbedClient

        client = FastEmbedClient()
        client.get_sentence_embedding_dimension()
        _fastembed_client = client
        return client


def _encode_with_client(client: Any, texts: Sequence[str]) -> List[List[float]]:
    vecs = client.encode(list(texts), normalize_embeddings=True)
    return vecs.tolist() if hasattr(vecs, "tolist") else [list(v) for v in vecs]


def _get_or_reset_collection(chroma_client, name: str, metadata: Dict[str, Any], client: Any):
    try:
        collection = chroma_client.get_collection(name)
    except Exception:
        return chroma_client.get_or_create_collection(name=name, metadata=metadata)

    current = collection.metadata or {}
    if not (
        current.get("embedding_fingerprint") not in (None, metadata["embedding_fingerprint"])
        or current.get("embedding_dimension") not in (None, metadata["embedding_dimension"])
        or current.get("embedding_lane") not in (None, metadata["embedding_lane"])
    ):
        return collection

    logger.info(
        "Recreating Chroma collection %s for embedding lane change (%s -> %s)",
        name,
        current.get("embedding_fingerprint"),
        metadata["embedding_fingerprint"],
    )
    preserved = {"ids": [], "documents": [], "metadatas": [], "embeddings": []}
    try:
        preserved = collection.get(include=["documents", "metadatas", "embeddings"]) or preserved
    except Exception as e:
        raise RuntimeError(f"Could not preserve documents before resetting {name}: {e}") from e

    ids = preserved.get("ids") or []
    docs = preserved.get("documents") or []
    metas = preserved.get("metadatas") or []
    prepared_batches = []
    if ids and docs:
        try:
            for start in range(0, len(ids), 100):
                batch_ids = ids[start:start + 100]
                batch_docs = docs[start:start + 100]
                batch_metas = metas[start:start + 100]
                if len(batch_metas) < len(batch_ids):
                    batch_metas += [{}] * (len(batch_ids) - len(batch_metas))
                prepared_batches.append((
                    batch_ids,
                    batch_docs,
                    batch_metas,
                    _encode_with_client(client, batch_docs),
                ))
        except Exception as e:
            raise RuntimeError(f"Could not re-embed preserved rows for {name}: {e}") from e

    chroma_client.delete_collection(name)
    collection = chroma_client.get_or_create_collection(name=name, metadata=metadata)

    try:
        for batch_ids, batch_docs, batch_metas, embeddings in prepared_batches:
            collection.add(
                ids=batch_ids,
                documents=batch_docs,
                metadatas=batch_metas,
                embeddings=embeddings,
            )
    except Exception as e:
        logger.warning("Could not write reset collection %s; restoring previous rows: %s", name, e)
        try:
            chroma_client.delete_collection(name)
            restored = chroma_client.get_or_create_collection(name=name, metadata=current)
            # chromadb returns embeddings as a numpy ndarray, whose truth value
            # is ambiguous — `preserved.get("embeddings") or []` and a bare
            # `if ... and old_embeddings:` both raise ValueError, which aborts
            # the restore and loses the rows the reset was supposed to keep.
            # Use explicit None/len checks instead.
            old_embeddings = preserved.get("embeddings")
            if old_embeddings is None:
                old_embeddings = []
            if ids and docs and len(old_embeddings):
                for start in range(0, len(ids), 100):
                    batch_ids = ids[start:start + 100]
                    batch_docs = docs[start:start + 100]
                    batch_metas = metas[start:start + 100]
                    batch_embeddings = old_embeddings[start:start + 100]
                    if hasattr(batch_embeddings, "tolist"):
                        batch_embeddings = batch_embeddings.tolist()
                    if len(batch_metas) < len(batch_ids):
                        batch_metas += [{}] * (len(batch_ids) - len(batch_metas))
                    restored.add(
                        ids=batch_ids,
                        documents=batch_docs,
                        metadatas=batch_metas,
                        embeddings=batch_embeddings,
                    )
        except Exception as restore_error:
            logger.warning("Could not restore previous collection %s: %s", name, restore_error)
        raise RuntimeError(f"Could not write reset collection {name}: {e}") from e
    if prepared_batches:
        logger.info("Re-embedded %s rows after resetting %s", len(ids), name)

    return collection


def _create_lane(chroma_client, base_name: str, lane_name: str, client: Any) -> EmbeddingLane:
    dimension = int(client.get_sentence_embedding_dimension())
    model = getattr(client, "model", "")
    url = getattr(client, "url", "")
    fp = _fingerprint(lane_name, url, model, dimension)
    name = collection_name(base_name, lane_name)
    metadata = _metadata(lane_name, url, model, dimension, fp)
    collection = _get_or_reset_collection(chroma_client, name, metadata, client)
    return EmbeddingLane(
        name=lane_name,
        client=client,
        collection=collection,
        collection_name=name,
        model=model,
        url=url,
        dimension=dimension,
        fingerprint=fp,
    )


def build_embedding_lanes(base_name: str) -> List[EmbeddingLane]:
    """Return the single supported local FastEmbed lane."""
    from src.chroma_client import get_chroma_client
    chroma_client = get_chroma_client()
    try:
        fastembed = _build_fastembed_client()
        return [_create_lane(chroma_client, base_name, LANE_FASTEMBED, fastembed)]
    except Exception as e:
        logger.error("FastEmbed retrieval lane unavailable for %s: %s", base_name, e)
        return []


def migrate_legacy_collection(base_name: str, lanes: Sequence[EmbeddingLane]) -> None:
    """Compatibility no-op: legacy unsuffixed collections are not read."""
    return

def primary_collection(lanes: Sequence[EmbeddingLane]):
    """The collection that pairs with the client the store embeds through.

    Every store keeps a single `_collection` for direct access and a single
    embedder, and picked them by different rules: the embedder was
    `lanes[0].client` (retrieval-preference order, so the custom endpoint when
    one is configured) while the collection preferred the FastEmbed lane. With
    both lanes up those are different models with different dimensions, so any
    caller embedding with one and querying the other gets a dimension error --
    latent only because nothing outside these modules reaches for the property
    today. Both now come from the same lane.
    """
    return lanes[0].collection if lanes else None


def lane_count(lanes: Sequence[EmbeddingLane]) -> int:
    return max((lane.count() for lane in lanes), default=0)


def dedupe_results(results: Iterable[Dict[str, Any]], id_key: str = "id", limit: Optional[int] = None) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for row in results:
        row_id = row.get(id_key)
        if not row_id or row_id in seen:
            continue
        seen.add(row_id)
        out.append(row)
        if limit is not None and len(out) >= limit:
            break
    return out


def query_lanes(
    lanes: Sequence[EmbeddingLane],
    query: str,
    n_results: Callable[[EmbeddingLane], int],
    include: Sequence[str],
    where: Optional[Dict[str, Any]] = None,
    where_document: Optional[Dict[str, Any]] = None,
    raise_if_all_failed: bool = False,
) -> List[tuple[EmbeddingLane, Dict[str, Any]]]:
    out: List[tuple[EmbeddingLane, Dict[str, Any]]] = []
    attempted = 0
    failures: List[str] = []
    for lane in lanes:
        try:
            count = lane.count()
            if count == 0:
                continue
            attempted += 1
            n = min(n_results(lane), count)
            if n <= 0:
                continue
            query_kwargs = {
                "query_embeddings": lane.encode([query]),
                "n_results": n,
                "where": where,
                "include": list(include),
            }
            # Only forward the document filter when one is asked for: passing
            # where_document=None is accepted by current Chroma but has been a
            # source of breakage across versions, and every existing caller
            # relies on the plain metadata-filtered query.
            if where_document is not None:
                query_kwargs["where_document"] = where_document
            results = lane.collection.query(**query_kwargs)
            out.append((lane, results))
        except Exception as e:
            failures.append(f"{lane.name}: {e}")
            logger.warning("%s lane query failed for %s: %s", lane.name, lane.collection_name, e)
    if raise_if_all_failed and attempted and not out and failures:
        raise RuntimeError("; ".join(failures))
    return out

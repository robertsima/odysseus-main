"""
rag_vector.py

Vector-based RAG using ChromaDB for storage and API-based embeddings.
Features: persistent storage, hybrid search (vector + keyword), sentence-aware chunking,
configurable embedding endpoint via EMBEDDING_URL env var.
"""

import os
import hashlib
import re
import logging
import numpy as np
from typing import List, Dict, Any, Optional, Set, Tuple

from src.constants import CHROMA_DIR
from src.index_walk import prune_index_dirs, is_indexable_file
from src.rag_sensitivity import (
    SENSITIVITY_KEY,
    SENSITIVITY_PRIVATE,
    SENSITIVITY_PUBLIC,
    apply_sensitivity,
    metadata_is_private,
    normalize_sensitivity,
)
from pathlib import Path

from src.embedding_lanes import (
    primary_collection,
    LANE_CUSTOM,
    LANE_FASTEMBED,
    build_embedding_lanes,
    collection_name,
    dedupe_results,
    lane_count,
    migrate_legacy_collection,
    query_lanes,
)
from src.rag_ranking import (
    cap_per_document,
    collect_link_targets,
    link_expansion_enabled,
    query_has_temporal_intent,
    query_tag_tokens,
    result_note_keys,
    tag_alias_score,
    temporal_factor,
)
from src.vault_markdown import (
    MARKDOWN_EXTENSIONS,
    build_chunk_header,
    chunk_markdown,
    encode_list,
    note_key as _note_key,
    parse_markdown,
)

logger = logging.getLogger(__name__)

DEFAULT_FILE_EXTENSIONS: Set[str] = {
    '.txt', '.md', '.markdown', '.py', '.json', '.yaml', '.yml',
    '.csv', '.html', '.css', '.js', '.pdf'
}

# Tool-internal directories that match DEFAULT_FILE_EXTENSIONS but are never
# Directory-walk pruning is single-sourced in src.index_walk so the vector and
# keyword indexers apply the same hidden/junk policy and cannot drift (#5559).

VECTOR_WEIGHT = 0.7
KEYWORD_WEIGHT = 0.3

# Score floor for a chunk whose file the query names outright. Above anything
# the weighted blend realistically produces for a document nobody asked for,
# while leaving headroom so named chunks stay ordered by vector similarity.
NAME_MATCH_FLOOR = 0.9

# How many top-ranked chunks get their vault links followed, and how many notes
# that may pull in. Both deliberately small: link expansion is a second round
# trip per lane, and a note's neighbours are supporting context, not the answer.
LINK_SEED_RESULTS = 3
MAX_LINK_TARGETS = 8
# Chunks reached only by a link are relevant *by association*. The discount
# keeps them below anything the query matched directly while still letting a
# strongly-similar linked note outrank a weak direct hit.
LINK_EXPANSION_DISCOUNT = 0.85

COLLECTION_NAME = "odysseus_rag"


def _generate_doc_id(text: str, owner: str = "") -> str:
    # Owner-scope the id so two owners can index byte-identical chunks
    # without the second one's add early-returning on the first's id and
    # being silently dropped from their owner-filtered search results.
    # Empty owner reproduces the legacy text-only id so the unowned/base
    # index keeps its existing ids and isn't re-churned.
    key = f"{owner}\x00{text}" if owner else text
    return f"doc_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


def _chunk_header(filename: str) -> str:
    """Provenance line prepended to every chunk before it is embedded.

    The filename is the only place a lot of vaults record what a document is
    *about* — a journal entry named ``08-08-2026.md`` typically never repeats
    its own date in the prose. Kept in metadata alone it is unreachable: the
    embedding is computed from chunk text, and the keyword half of the hybrid
    score in ``search`` matches against chunk text too. So the name has to be
    *in* the text, on every chunk rather than just the first, or a query naming
    the file only ever hits whichever chunk happens to mention it.

    Both the full name and its extensionless stem are emitted because
    ``search`` tokenises by bare ``str.split()``: ``08-08-2026.md`` is a single
    token that a query saying "08-08-2026" does not match. The stem supplies
    that token. For names that already read as prose ("Odysseus Reference.md")
    the two overlap almost entirely, which costs a few tokens and no accuracy.
    """
    stem = Path(filename).stem
    if stem and stem != filename:
        return f"Source: {filename} {stem}"
    return f"Source: {filename}"


# Words too common to mean the user is naming a document. Kept deliberately
# short: this only has to stop an accidental full-credit match, and every entry
# it misses still scores through the ordinary keyword path.
_NAME_MATCH_STOPWORDS = {
    "and", "are", "for", "from", "how", "not", "the", "was", "what", "when",
    "where", "which", "who", "why", "with", "you", "your", "new", "old",
    "doc", "docs", "file", "files", "note", "notes", "index", "readme",
}


# How many name lookups one search may spend. Each is an extra round trip, and
# a query naming more than a couple of documents is not a real pattern.
_MAX_NAMED_DOCUMENT_TOKENS = 2


def _distinctive_query_tokens(query_words: set) -> list:
    """Query tokens that look like they *name* something rather than describe it.

    Restricted to tokens carrying a digit — "08-08-2026", "v2.1", "ticket-442".
    Those are precisely the tokens embeddings handle worst: an opaque
    identifier carries almost no semantic signal, so the document it names sits
    far from the query in vector space no matter how relevant it is. An
    ordinary word like "architecture" needs no special handling, because it is
    already near its document and arrives through the normal pass.

    Longest first, so the full date beats the bare year when both appear.
    """
    named = [
        token for token in query_words
        if len(token) >= 4
        and token not in _NAME_MATCH_STOPWORDS
        and any(char.isdigit() for char in token)
    ]
    named.sort(key=lambda token: (-len(token), token))
    return named[:_MAX_NAMED_DOCUMENT_TOKENS]


def _query_names_document(query_words: set, meta: Any) -> bool:
    """True when the query appears to name this chunk's source file.

    Naming a document is a far stronger signal of intent than happening to
    share a word with its prose, but the plain keyword score cannot express
    that: it divides matches by the query length, so the one token that
    uniquely identifies a file ("08-08-2026" in a thirteen-word question) is
    worth 1/13 of the keyword weight — a rounding error next to the vector
    gaps between candidates. Treating a name match as full keyword credit
    lifts the named document without touching how anything else is scored.

    Matching is on the stem and its parts, so "08-08-2026" resolves for a
    query saying either the whole date or just the year. Parts shorter than
    three characters are ignored, which drops the "08" fragments that would
    otherwise match any month or day.
    """
    if not query_words or not isinstance(meta, dict):
        return False
    filename = meta.get("filename")
    if not isinstance(filename, str) or not filename:
        return False

    stem = Path(filename).stem.lower()
    if not stem:
        return False
    candidates = {stem}
    candidates.update(part for part in re.split(r"[\s._\-]+", stem) if part)

    for token in candidates:
        if len(token) < 3 or token in _NAME_MATCH_STOPWORDS:
            continue
        if token in query_words:
            return True
    return False


def _build_where(owner: Optional[str], allow_private: bool) -> Optional[Dict[str, Any]]:
    """Compose the Chroma metadata filter for an owner + sensitivity scope.

    ``allow_private=False`` matches ``sensitivity == "public"`` by equality
    rather than excluding ``"private"``. That relies on every chunk carrying the
    key, which ``apply_sensitivity`` guarantees on write and
    ``backfill_sensitivity`` guarantees for chunks written before the label
    existed — no dependence on how a given Chroma version treats documents that
    are missing a filtered field.
    """
    clauses = []
    if owner:
        clauses.append({"owner": owner})
    if not allow_private:
        clauses.append({SENSITIVITY_KEY: SENSITIVITY_PUBLIC})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _with_clause(
    where: Optional[Dict[str, Any]], clause: Dict[str, Any]
) -> Dict[str, Any]:
    """Add a clause to a ``_build_where`` filter without losing its scoping.

    Link expansion narrows to a set of notes, but it must stay inside the same
    owner and sensitivity scope as the primary pass — a second query that
    dropped those would reach private notes from a hosted-API turn.
    """
    if not where:
        return clause
    if "$and" in where and isinstance(where["$and"], list):
        return {"$and": list(where["$and"]) + [clause]}
    return {"$and": [where, clause]}


def _rewrite_owner_path(value: str, path_map: Dict[str, str], path_prefixes: List[tuple]) -> str:
    if not isinstance(value, str) or not value:
        return value
    abs_value = os.path.abspath(value)
    mapped = path_map.get(abs_value)
    if mapped:
        return mapped
    for old_prefix, new_prefix in path_prefixes:
        old_abs = os.path.abspath(old_prefix)
        new_abs = os.path.abspath(new_prefix)
        if abs_value == old_abs:
            return new_abs
        if abs_value.startswith(old_abs + os.sep):
            return new_abs + abs_value[len(old_abs):]
    return value


class VectorRAG:
    """RAG system using ChromaDB vector storage with hybrid search."""

    def __init__(self, persist_directory: str = CHROMA_DIR):
        self.persist_directory = persist_directory
        self._collection = None
        self._model = None
        self._lanes = []
        self._healthy = False

        Path(self.persist_directory).mkdir(parents=True, exist_ok=True)
        self._initialize_system()

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _initialize_system(self) -> bool:
        try:
            self._lanes = build_embedding_lanes(COLLECTION_NAME)
            if not self._lanes:
                raise RuntimeError("No embedding lanes available")
            self._collection = primary_collection(self._lanes)
            self._model = self._lanes[0].client
            migrate_legacy_collection(COLLECTION_NAME, self._lanes)
            try:
                self.backfill_sensitivity()
            except Exception as e:
                # Never fail init over the backfill: an unlabeled index still
                # works for local sessions, and the label is re-attempted next
                # start.
                logger.warning("sensitivity backfill skipped: %s", e)
            logger.info(
                "VectorRAG ready (lanes=%s docs=%s)",
                [lane.name for lane in self._lanes],
                lane_count(self._lanes),
            )
            self._healthy = True
            return True

        except Exception as e:
            logger.error(f"VectorRAG init failed: {e}")
            self._healthy = False
            return False

    def _embed(self, texts: List[str]) -> List[List[float]]:
        if not self._lanes:
            return []
        return np.array(self._lanes[0].encode(texts), dtype=np.float32).tolist()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def healthy(self) -> bool:
        if getattr(self, "_lanes", None):
            return self._healthy and bool(self._lanes)
        return self._healthy and getattr(self, "_collection", None) is not None

    @property
    def collection(self):
        """Expose the ChromaDB collection for direct access by personal_routes etc."""
        return self._collection

    def _active_collections(self):
        lanes = getattr(self, "_lanes", None)
        if lanes:
            return [(lane.name, lane.collection) for lane in lanes]
        collection = getattr(self, "_collection", None)
        return [("legacy", collection)] if collection is not None else []

    def _collections_for_delete(self):
        collections = []
        seen = set()

        def add(lane_name: str, collection) -> None:
            if collection is None:
                return
            key = getattr(collection, "name", None) or id(collection)
            if key in seen:
                return
            seen.add(key)
            collections.append((lane_name, collection))

        for lane_name, collection in self._active_collections():
            add(lane_name, collection)

        if getattr(self, "_lanes", None):
            try:
                from src.chroma_client import get_chroma_client

                client = get_chroma_client()
                try:
                    add("legacy", client.get_collection(COLLECTION_NAME))
                except Exception:
                    pass
                for lane_name in (LANE_CUSTOM, LANE_FASTEMBED):
                    try:
                        add(lane_name, client.get_collection(collection_name(COLLECTION_NAME, lane_name)))
                    except Exception:
                        pass
            except Exception:
                pass

        return collections

    # ------------------------------------------------------------------
    # Document operations
    # ------------------------------------------------------------------

    def _promote_to_private(self, lane, doc_id: str, existing: Dict[str, Any]) -> bool:
        """Relabel an already-stored chunk private, leaving its other metadata.

        Used when re-indexing the same content under a stricter label. Keeps the
        original ``source``/``owner`` so provenance still points at the first
        copy indexed; only the sensitivity changes.
        """
        stored = (existing.get("metadatas") or [None])
        stored = stored[0] if stored else None
        if not isinstance(stored, dict):
            stored = {}
        if metadata_is_private(stored):
            return False
        try:
            lane.collection.update(
                ids=[doc_id],
                metadatas=[apply_sensitivity(stored, SENSITIVITY_PRIVATE)],
            )
            return True
        except Exception as e:
            logger.warning("failed to promote %s to private in %s lane: %s", doc_id, lane.name, e)
            return False

    def add_document(self, text: str, metadata: Dict[str, Any]) -> bool:
        if not self.healthy:
            logger.error("Collection not initialized")
            return False
        if not text or not isinstance(text, str):
            return False
        if not metadata or not isinstance(metadata, dict):
            return False

        # Normalize here rather than at each call site so every chunk reaching
        # the store carries a sensitivity label, whatever wrote it (directory
        # indexing, direct uploads, attachment capture).
        metadata = apply_sensitivity(metadata)
        doc_id = _generate_doc_id(text, metadata.get("owner") or "")
        wrote = False
        for lane in self._lanes:
            try:
                existing = lane.collection.get(ids=[doc_id])
                if existing["ids"]:
                    # Ids are content-derived, so the same text indexed twice —
                    # e.g. a vault indexed as public, then its Private/ subfolder
                    # indexed as private — lands here and the original write's
                    # label would stand. Private has to win, or the more
                    # restrictive pass is silently discarded and the content
                    # stays reachable from hosted models. Restricting further is
                    # always the safe direction; public never overwrites private.
                    if metadata_is_private(metadata):
                        self._promote_to_private(lane, doc_id, existing)
                    wrote = True
                    continue
                lane.collection.add(
                    ids=[doc_id],
                    embeddings=lane.encode([text]),
                    documents=[text],
                    metadatas=[metadata],
                )
                wrote = True
            except Exception as e:
                logger.warning("add_document failed in %s lane: %s", lane.name, e)
        return wrote

    def add_documents_batch(self, docs: List[tuple]) -> Dict[str, Any]:
        if not self.healthy:
            return {"success": False, "message": "Collection not initialized"}
        if not docs:
            return {"success": False, "message": "Empty document list"}

        valid = [
            (t, apply_sensitivity(m)) for t, m in docs
            if t and isinstance(t, str) and m and isinstance(m, dict)
        ]
        if not valid:
            return {"success": False, "message": "No valid documents"}

        added_ids = set()
        attempted_new = False
        write_failed = False
        for lane in self._lanes:
            all_ids = [_generate_doc_id(t, m.get("owner") or "") for t, m in valid]
            try:
                existing = lane.collection.get(ids=all_ids)
                existing_ids = set(existing.get("ids") or [])
                existing_metas = dict(
                    zip(existing.get("ids") or [], existing.get("metadatas") or [])
                )
            except Exception:
                existing_ids = set()
                existing_metas = {}

            # Same private-wins rule as add_document: a skipped duplicate must
            # still be able to tighten an existing chunk's label.
            for (text, meta), doc_id in zip(valid, all_ids):
                if doc_id in existing_ids and metadata_is_private(meta):
                    self._promote_to_private(
                        lane, doc_id, {"metadatas": [existing_metas.get(doc_id)]}
                    )

            new_texts = []
            new_metas = []
            new_ids = []
            for (text, meta), doc_id in zip(valid, all_ids):
                if doc_id not in existing_ids:
                    new_texts.append(text)
                    new_metas.append(meta)
                    new_ids.append(doc_id)

            if new_texts:
                attempted_new = True
                lane_failed = False
                for i in range(0, len(new_texts), 100):
                    batch_texts = new_texts[i:i + 100]
                    batch_ids = new_ids[i:i + 100]
                    batch_metas = new_metas[i:i + 100]
                    try:
                        lane.collection.add(
                            ids=batch_ids,
                            embeddings=lane.encode(batch_texts),
                            documents=batch_texts,
                            metadatas=batch_metas,
                        )
                    except Exception as e:
                        lane_failed = True
                        write_failed = True
                        logger.warning("add_documents_batch failed in %s lane: %s", lane.name, e)
                        break
                if not lane_failed:
                    added_ids.update(new_ids)

        if attempted_new and write_failed and not added_ids:
            return {"success": False, "message": "No embedding lane accepted the batch"}

        return {
            "success": True,
            "added_count": len(added_ids),
            "total_count": len(docs),
            "failed_count": len(docs) - len(valid),
        }

    def rename_owner(
        self,
        old_owner: str,
        new_owner: str,
        *,
        path_map: Optional[Dict[str, str]] = None,
        path_prefixes: Optional[List[tuple]] = None,
    ) -> Dict[str, Any]:
        """Rewrite existing RAG metadata after an auth username rename."""
        if not self.healthy:
            return {"success": False, "updated_count": 0, "message": "Collection not initialized"}

        old_owner = (old_owner or "").strip().lower()
        new_owner = (new_owner or "").strip().lower()
        if not old_owner or not new_owner or old_owner == new_owner:
            return {"success": True, "updated_count": 0, "message": "No owner rename needed"}

        path_map = {os.path.abspath(k): os.path.abspath(v) for k, v in (path_map or {}).items()}
        path_prefixes = path_prefixes or []
        updated_ids = set()
        failed_count = 0

        for lane_name, collection in self._collections_for_delete():
            try:
                results = collection.get(
                    where={"owner": old_owner},
                    include=["metadatas"],
                )
            except Exception as e:
                logger.warning("rename_owner metadata scan failed in %s lane: %s", lane_name, e)
                failed_count += 1
                continue

            ids = results.get("ids") or []
            metadatas = results.get("metadatas") or []
            if not ids:
                continue

            new_metas = []
            selected_ids = []
            for doc_id, meta in zip(ids, metadatas):
                if not isinstance(meta, dict):
                    continue
                next_meta = dict(meta)
                if str(next_meta.get("owner", "")).strip().lower() == old_owner:
                    next_meta["owner"] = new_owner
                for key in ("source", "directory"):
                    next_meta[key] = _rewrite_owner_path(next_meta.get(key), path_map, path_prefixes)
                selected_ids.append(doc_id)
                new_metas.append(next_meta)

            if not selected_ids:
                continue

            try:
                collection.update(ids=selected_ids, metadatas=new_metas)
                updated_ids.update(selected_ids)
            except Exception as e:
                logger.warning("rename_owner metadata update failed in %s lane: %s", lane_name, e)
                failed_count += len(selected_ids)

        success = failed_count == 0
        return {
            "success": success,
            "updated_count": len(updated_ids),
            "failed_count": failed_count,
            "message": f"Updated {len(updated_ids)} RAG chunk(s)",
        }

    # ------------------------------------------------------------------
    # Search — hybrid: vector similarity + keyword overlap
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        k: int = 5,
        owner: Optional[str] = None,
        allow_private: bool = True,
    ) -> List[Dict[str, Any]]:
        """Hybrid search, optionally scoped to owner and to public-only chunks.

        ``allow_private=False`` is what keeps notes marked private from being
        pasted into a prompt bound for a hosted API — callers derive it from
        the session's endpoint (see ``model_context.is_local_endpoint``).
        """
        if not self.healthy:
            return []
        if not query or not isinstance(query, str):
            return []
        if lane_count(self._lanes) == 0:
            return []

        try:
            where_filter = _build_where(owner, allow_private)
            query_words = set(query.lower().split())
            temporal_intent = query_has_temporal_intent(query)
            candidates = []

            seen_ids = set()

            def collect(lane, results, *, path: str = "direct", discount: float = 1.0):
                for idx in range(len(results["ids"][0])):
                    doc_id = results["ids"][0][idx]
                    if doc_id in seen_ids:
                        continue
                    seen_ids.add(doc_id)
                    distance = results["distances"][0][idx]
                    doc_text = results["documents"][0][idx]
                    meta = results["metadatas"][0][idx]

                    vector_sim = 1.0 - distance
                    doc_words = set(doc_text.lower().split())
                    overlap = len(query_words & doc_words)
                    keyword_score = overlap / len(query_words) if query_words else 0.0
                    # A query naming a tag or alias is naming a *set* of notes
                    # deliberately. Prose overlap barely registers that — the
                    # tag may appear once in a frontmatter block — so take
                    # whichever signal is stronger rather than averaging them.
                    keyword_score = max(keyword_score, tag_alias_score(query, query_words, meta))
                    named = _query_names_document(query_words, meta)
                    if named:
                        keyword_score = 1.0
                    hybrid_score = (VECTOR_WEIGHT * vector_sim) + (KEYWORD_WEIGHT * keyword_score)
                    # Recency multiplies the blend, and — below — the name
                    # floor's tie-break term as well. It has to reach both:
                    # "Deploy.md" and "Deploy 2022.md" are *both* named by a
                    # query saying "deploy", so the floor puts them level and
                    # the date is the only thing left that says which one is
                    # still true.
                    temporal = temporal_factor(meta, temporal_intent)
                    hybrid_score *= temporal
                    if discount != 1.0:
                        hybrid_score *= discount
                    if named:
                        # Full keyword credit alone tops out at +0.3, which a
                        # weak vector match still loses to — and the documents
                        # that most need naming are exactly the ones embeddings
                        # place badly. Naming a file is an explicit request for
                        # it, so float it above everything unnamed and let
                        # vector similarity, discounted by age, order the named
                        # ones among themselves. The floor itself is untouched
                        # by recency, so an old note the user named by name is
                        # still returned ahead of every note they did not.
                        hybrid_score = max(
                            hybrid_score,
                            NAME_MATCH_FLOOR + (1.0 - NAME_MATCH_FLOOR) * vector_sim * temporal,
                        )

                    candidates.append({
                        "id": doc_id,
                        "document": doc_text,
                        "metadata": meta,
                        "distance": round(distance, 4),
                        "similarity": round(hybrid_score, 4),
                        "vector_similarity": round(vector_sim, 4),
                        "keyword_score": round(keyword_score, 4),
                        "embedding_lane": lane.name,
                        "retrieval_path": path,
                    })

            for lane, results in query_lanes(
                self._lanes,
                query,
                n_results=lambda lane: min(
                    (k * 6 if (owner or not allow_private) else k * 3),
                    max(k, 20),
                    lane.count(),
                ),
                where=where_filter,
                include=["documents", "metadatas", "distances"],
                raise_if_all_failed=True,
            ):
                collect(lane, results)

            # The pass above only ever sees the ~20 nearest chunks by embedding.
            # That is fatal for the case this whole feature exists to serve: a
            # journal entry named for its date is, in prose, about whatever
            # happened that day, so "what did I write on 08-08-2026" lands
            # nowhere near it in vector space and the file never enters the
            # pool at all. Re-ranking cannot rescue a document that was never
            # retrieved, so fetch it directly: the provenance header written by
            # _chunk_header / build_chunk_header guarantees the file name
            # appears in the chunk text, which makes a substring filter an
            # exact way to find it.
            for token in _distinctive_query_tokens(query_words):
                try:
                    for lane, results in query_lanes(
                        self._lanes,
                        query,
                        n_results=lambda lane: min(k, lane.count()),
                        where=where_filter,
                        where_document={"$contains": token},
                        include=["documents", "metadatas", "distances"],
                    ):
                        collect(lane, results)
                except Exception as e:
                    # A backend without document filtering must not take the
                    # ordinary search down with it.
                    logger.debug("named-document pass for %r failed: %s", token, e)
                    break

            candidates.sort(key=lambda c: c["similarity"], reverse=True)

            # Multi-source pass. The two passes above can only return notes the
            # query itself resembles, which is exactly wrong for a vault: the
            # constraint that makes an answer correct is routinely recorded in a
            # *neighbouring* note ("see [[Retention Policy]]") that shares no
            # vocabulary with the question. The wikilinks are the user's own
            # statement that those notes belong together, so follow them.
            self._expand_by_links(
                candidates, query, k, where_filter, collect
            )

            candidates.sort(key=lambda c: c["similarity"], reverse=True)
            # Breadth before depth: at most a couple of chunks from any one
            # file, so a long note cannot spend the whole context budget and
            # hide the second source that would have shown a conflict.
            #
            # Unless the query named what it wants. "#homelab" or a filename is
            # the user pointing at specific notes, and forcing breadth there
            # trades their best passages for weaker ones from notes nobody
            # asked about. Deciding it here rather than inside collect() keeps
            # the per-candidate loop untouched; re-checking the name match over
            # the final shortlist is a few dozen string comparisons.
            focused = bool(query_tag_tokens(query)) or any(
                _query_names_document(query_words, c.get("metadata"))
                for c in candidates
            )
            top = cap_per_document(dedupe_results(candidates), limit=k, focused=focused)
            logger.info(f"Hybrid search for '{query[:60]}': {len(top)} results")
            return top

        except Exception as e:
            logger.error(f"search failed: {e}")
            return self._keyword_search_fallback(query, k, owner=owner, allow_private=allow_private)

    def _expand_by_links(
        self,
        candidates: List[Dict[str, Any]],
        query: str,
        k: int,
        where_filter: Optional[Dict[str, Any]],
        collect,
    ) -> None:
        """Pull in chunks from notes the top results link to, at a discount.

        No-ops on an index without link metadata (nothing was written before
        markdown-aware indexing existed), and on any backend that rejects the
        ``$in`` filter — a missing supporting note is a worse answer, but a
        failed primary search is no answer at all.
        """
        if not link_expansion_enabled() or not candidates:
            return
        targets = collect_link_targets(
            candidates, LINK_SEED_RESULTS, exclude=result_note_keys(candidates)
        )[:MAX_LINK_TARGETS]
        if not targets:
            return
        try:
            for lane, results in query_lanes(
                self._lanes,
                query,
                n_results=lambda lane: min(k, lane.count()),
                where=_with_clause(where_filter, {"note_key": {"$in": targets}}),
                include=["documents", "metadatas", "distances"],
            ):
                collect(lane, results, path="link", discount=LINK_EXPANSION_DISCOUNT)
        except Exception as e:
            logger.debug("link expansion pass failed: %s", e)

    def _keyword_search_fallback(
        self,
        query: str,
        k: int = 5,
        owner: Optional[str] = None,
        allow_private: bool = True,
    ) -> List[Dict[str, Any]]:
        """Python-side scan used when every lane's vector query fails.

        It re-implements the owner and sensitivity scoping of ``search`` because
        it bypasses Chroma's ``where`` filter entirely — without that, a lane
        outage would turn into a private-content leak.
        """
        try:
            if not self._active_collections():
                return []

            query_words = query.lower().split()
            scored = []
            for lane_name, collection in self._active_collections():
                if collection.count() == 0:
                    continue
                all_docs = collection.get(include=["documents", "metadatas"])
                if not all_docs["ids"]:
                    continue
                for i, doc in enumerate(all_docs["documents"]):
                    meta = all_docs["metadatas"][i]
                    if owner and meta.get("owner") != owner:
                        continue
                    if not allow_private and metadata_is_private(meta):
                        continue
                    doc_lower = doc.lower()
                    score = sum(1 for w in query_words if w in doc_lower)
                    if score > 0:
                        scored.append({
                            "id": all_docs["ids"][i],
                            "document": doc,
                            "metadata": meta,
                            "distance": 0,
                            "similarity": score,
                            "search_type": "keyword_fallback",
                            "embedding_lane": lane_name,
                        })

            scored.sort(key=lambda x: x["similarity"], reverse=True)
            return dedupe_results(scored, limit=k)
        except Exception as e:
            logger.error(f"keyword fallback failed: {e}")
            return []

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def backfill_sensitivity(self) -> Dict[str, Any]:
        """Label chunks written before the sensitivity field existed as public.

        Public-only search filters by equality on the label, so an unlabeled
        chunk would otherwise disappear from every hosted-API session — a
        silent regression for indexes that predate this feature. Runs at init;
        a second run finds nothing and costs one metadata-only read per lane.
        """
        updated = 0
        for lane_name, collection in self._active_collections():
            try:
                if collection.count() == 0:
                    continue
                existing = collection.get(include=["metadatas"])
                ids = existing.get("ids") or []
                metas = existing.get("metadatas") or []

                pending_ids = []
                pending_metas = []
                for doc_id, meta in zip(ids, metas):
                    if isinstance(meta, dict) and SENSITIVITY_KEY in meta:
                        continue
                    pending_ids.append(doc_id)
                    pending_metas.append(apply_sensitivity(meta if isinstance(meta, dict) else {}))

                for i in range(0, len(pending_ids), 200):
                    collection.update(
                        ids=pending_ids[i:i + 200],
                        metadatas=pending_metas[i:i + 200],
                    )
                    updated += len(pending_ids[i:i + 200])
            except Exception as e:
                logger.warning("sensitivity backfill failed in %s lane: %s", lane_name, e)

        if updated:
            logger.info("Labelled %s pre-existing RAG chunk(s) as %s", updated, SENSITIVITY_PUBLIC)
        return {"updated_count": updated}

    def rebuild_index(self) -> bool:
        try:
            from src.chroma_client import get_chroma_client
            client = get_chroma_client()
            try:
                client.delete_collection(COLLECTION_NAME)
            except Exception:
                pass
            for name in (
                collection_name(COLLECTION_NAME, LANE_CUSTOM),
                collection_name(COLLECTION_NAME, LANE_FASTEMBED),
            ):
                try:
                    client.delete_collection(name)
                except Exception:
                    pass
            # Rebuild means empty current lanes. Clear the legacy unsuffixed
            # collection too so startup migration cannot resurrect stale docs.
            self._lanes = build_embedding_lanes(COLLECTION_NAME)
            self._collection = primary_collection(self._lanes)
            self._healthy = True
            return True
        except Exception as e:
            logger.error(f"rebuild_index failed: {e}")
            self._healthy = False
            return False

    def get_stats(self) -> Dict[str, Any]:
        if not self.healthy:
            return {"error": "Collection not initialized"}
        try:
            return {
                "document_count": lane_count(self._lanes),
                "embedding_model": f"{self._lanes[0].model} @ {self._lanes[0].url}" if self._lanes else "N/A",
                "persist_directory": self.persist_directory,
                "collection_name": COLLECTION_NAME,
                "embedding_lanes": [lane.stats() for lane in self._lanes],
                "healthy": True,
            }
        except Exception as e:
            logger.error(f"get_stats failed: {e}")
            return {"error": str(e), "healthy": False}

    # ------------------------------------------------------------------
    # Directory indexing
    # ------------------------------------------------------------------

    def index_personal_documents(
        self,
        directory: str,
        file_extensions: Optional[set] = None,
        owner: Optional[str] = None,
        sensitivity: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Index a directory tree. ``sensitivity`` labels every chunk it writes;
        omitted means ``public`` (see ``rag_sensitivity``)."""
        if file_extensions is None:
            file_extensions = DEFAULT_FILE_EXTENSIONS

        indexed = 0
        failed = 0

        try:
            for root, dirs, files in os.walk(directory):
                # Prune in place so os.walk never descends into hidden or junk
                # directories (#5559), via the shared index_walk policy. The
                # passed-in root is exempt: a user who deliberately targets a
                # hidden directory gets it.
                prune_index_dirs(dirs)
                for fname in files:
                    if not is_indexable_file(fname):
                        continue
                    fpath = os.path.join(root, fname)
                    ext = Path(fname).suffix.lower()
                    if ext not in file_extensions:
                        continue

                    ok, bad = self.index_file(fpath, owner=owner, sensitivity=sensitivity)
                    indexed += ok
                    failed += bad

            return {
                'success': True,
                'indexed_count': indexed,
                'failed_count': failed,
                'message': f'Indexed {indexed} chunks from {directory}',
            }
        except Exception as e:
            logger.error(f"index_personal_documents {directory}: {e}")
            return {'success': False, 'indexed_count': indexed, 'failed_count': failed, 'message': str(e)}

    def index_file(
        self,
        path: str,
        owner: Optional[str] = None,
        sensitivity: Optional[str] = None,
    ) -> Tuple[int, int]:
        """Index a single file's chunks. Returns ``(indexed, failed)``.

        Split out of ``index_personal_documents`` so the incremental vault scan
        can re-index one changed file without walking (and re-embedding) the
        whole tree.
        """
        try:
            fname = os.path.basename(path)
            ext = Path(fname).suffix.lower()
            if ext == '.pdf':
                from src.personal_docs import extract_pdf_text
                content = extract_pdf_text(path)
            else:
                with open(path, 'r', encoding='utf-8') as handle:
                    content = handle.read()

            if not content or not content.strip():
                return (0, 0)

            meta = apply_sensitivity({
                'source': path,
                'filename': fname,
                'directory': os.path.dirname(path),
                'type': ext,
            }, sensitivity)
            if owner:
                meta['owner'] = owner

            if ext in MARKDOWN_EXTENSIONS:
                chunks = self._markdown_chunks(path, fname, content, meta)
            else:
                header = _chunk_header(fname)
                chunks = [
                    (f"{header}\n{chunk}", {**meta, 'chunk_id': i})
                    for i, chunk in enumerate(self._split_into_chunks(content))
                ]
            if not chunks:
                return (0, 0)

            # One batched write instead of a get+add round-trip per chunk.
            # The incremental vault scan re-indexes a whole file on every save,
            # so a medium note was costing ~26 sequential HTTP calls to Chroma
            # each time it was edited -- which dominated the cost of saving a
            # rolling report that gets rewritten on every update.
            result = self.add_documents_batch(chunks)
            if not result.get('success'):
                return (0, len(chunks))
            # add_documents_batch counts only NEW ids in added_count, but a
            # chunk that was already present is not a failure: add_document
            # returned True for that case, and index_personal_documents
            # re-walks already-indexed trees where every chunk is a duplicate.
            # Only genuinely malformed chunks count as failed.
            failed = int(result.get('failed_count') or 0)
            return (len(chunks) - failed, failed)
        except Exception as e:
            logger.error(f"index {path}: {e}")
            return (0, 1)

    def _markdown_chunks(
        self, path: str, fname: str, content: str, meta: Dict[str, Any]
    ) -> List[tuple]:
        """Chunk a note along its headings, carrying its vault metadata.

        Falls back to the plain sentence splitter if anything in the Markdown
        layer raises: a note that cannot be parsed as Obsidian is still a note,
        and losing it from the index entirely would be a far worse outcome than
        indexing it without tags.
        """
        try:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = None
            doc = parse_markdown(content, fname, mtime=mtime)
            pieces = chunk_markdown(doc, self._split_into_chunks)
            if not pieces:
                return []

            doc_meta = {
                **meta,
                'title': doc.title,
                'note_key': doc.key,
                'tags': encode_list(doc.tags),
                'aliases': encode_list(doc.aliases),
                'links': encode_list(doc.links),
                'doc_date': float(doc.doc_date) if doc.doc_date else 0.0,
                'doc_date_source': doc.doc_date_source,
            }
            out = []
            for i, piece in enumerate(pieces):
                header = build_chunk_header(
                    fname,
                    heading_path=piece.heading_path,
                    tags=doc.tags,
                    aliases=doc.aliases,
                    doc_date=doc.doc_date,
                )
                out.append((
                    f"{header}\n{piece.text}",
                    {**doc_meta, 'chunk_id': i, 'heading_path': piece.heading_path},
                ))
            return out
        except Exception as e:
            logger.warning("markdown parse failed for %s (%s); indexing as plain text", path, e)
            header = _chunk_header(fname)
            return [
                (f"{header}\n{chunk}", {**meta, 'chunk_id': i, 'note_key': _note_key(fname)})
                for i, chunk in enumerate(self._split_into_chunks(content))
            ]

    def owner_for_directory(self, directory: str) -> Optional[str]:
        """Owner recorded on chunks already indexed from ``directory``.

        Re-indexing has to preserve the owner or the new chunks fall outside the
        owner-filtered search and become unretrievable. Reading it back from the
        existing chunks avoids a second persisted mapping and works for
        directories added before the incremental scan existed.
        """
        if not self.healthy:
            return None
        directory = os.path.abspath(directory)
        try:
            for _lane_name, collection in self._active_collections():
                if collection.count() == 0:
                    continue
                got = collection.get(include=["metadatas"])
                for meta in got.get("metadatas") or []:
                    if not isinstance(meta, dict):
                        continue
                    source = meta.get("source")
                    owner = meta.get("owner")
                    if not owner or not isinstance(source, str):
                        continue
                    if source == directory or source.startswith(directory + os.sep):
                        return owner
        except Exception as e:
            logger.warning("owner_for_directory(%s) failed: %s", directory, e)
        return None

    def set_directory_sensitivity(self, directory: str, sensitivity: str) -> Dict[str, Any]:
        """Relabel every chunk indexed from ``directory`` (recursively).

        Selection uses the same Python-side path-boundary match on the stored
        ``source`` as ``remove_directory``, and for the same reason: no Chroma
        metadata operator selects a scalar string by path prefix, and a plain
        substring would catch ``/docs2`` when relabelling ``/docs``.
        """
        if not self.healthy:
            return {"success": False, "updated_count": 0, "message": "Collection not initialized"}

        directory = os.path.abspath(directory)
        label = normalize_sensitivity(sensitivity)
        updated = 0
        failed = 0

        for lane_name, collection in self._collections_for_delete():
            try:
                results = collection.get(include=["metadatas"])
                selected_ids = []
                selected_metas = []
                for i, meta in enumerate(results["metadatas"]):
                    if not isinstance(meta, dict) or not isinstance(meta.get("source"), str):
                        continue
                    source = meta["source"]
                    if source != directory and not source.startswith(directory + os.sep):
                        continue
                    if meta.get(SENSITIVITY_KEY) == label:
                        continue
                    selected_ids.append(results["ids"][i])
                    selected_metas.append(apply_sensitivity(meta, label))

                for i in range(0, len(selected_ids), 200):
                    collection.update(
                        ids=selected_ids[i:i + 200],
                        metadatas=selected_metas[i:i + 200],
                    )
                    updated += len(selected_ids[i:i + 200])
            except Exception as e:
                logger.warning("set_directory_sensitivity failed in %s lane: %s", lane_name, e)
                failed += 1

        return {
            "success": failed == 0,
            "updated_count": updated,
            "sensitivity": label,
            "message": f"Relabelled {updated} chunk(s) under {directory} as {label}",
        }

    def remove_directory(self, directory: str) -> Dict[str, Any]:
        """Remove all chunks under ``directory`` (recursively), and nothing else.

        Selection is a Python-side path-boundary match on each chunk's stored
        ``source`` full path, NOT a Chroma metadata ``where`` filter. No Chroma
        metadata operator selects a scalar string by path prefix (``$contains``
        targets document content / list membership, not a ``source`` substring),
        and a plain substring would over-delete siblings — removing ``/docs``
        must not touch ``/docs2`` or ``/docs_personal``. We therefore match
        ``source == directory`` or ``source`` startswith ``directory + os.sep``,
        the same boundary rule add_directory uses for exclusions. ``directory``
        is abspath-normalized so it matches the absolute ``source`` that indexing
        always stores, regardless of how the caller passed it in.
        """
        if not self.healthy:
            return {"success": False, "message": "Collection not initialized"}
        directory = os.path.abspath(directory)
        try:
            removed_ids = set()
            for _lane_name, collection in self._collections_for_delete():
                results = collection.get(include=["metadatas"])
                ids = [
                    results["ids"][i]
                    for i, m in enumerate(results["metadatas"])
                    if isinstance(m, dict)
                    and isinstance(m.get("source"), str)
                    and (m["source"] == directory or m["source"].startswith(directory + os.sep))
                ]
                if ids:
                    collection.delete(ids=ids)
                    removed_ids.update(ids)
            if not removed_ids:
                return {"success": True, "removed_count": 0, "message": "No docs found"}

            n = len(removed_ids)
            logger.info(f"Removed {n} chunks from {directory}")
            return {"success": True, "removed_count": n, "message": f"Removed {n} chunks"}
        except Exception as e:
            logger.error(f"remove_directory {directory}: {e}")
            return {"success": False, "message": str(e)}

    def reindex_directory(
        self, directory: str, file_extensions: Optional[set] = None
    ) -> Dict[str, Any]:
        remove_result = self.remove_directory(directory)
        if not remove_result.get("success"):
            return remove_result
        index_result = self.index_personal_documents(directory, file_extensions)
        return {
            "success": index_result.get("success", False),
            "message": (
                f"Re-index for {directory}: removed {remove_result.get('removed_count', 0)}, "
                f"{index_result.get('message', '')}"
            ),
            "removed_count": remove_result.get("removed_count", 0),
            "indexed_count": index_result.get("indexed_count", 0),
            "failed_count": index_result.get("failed_count", 0),
        }

    # ------------------------------------------------------------------
    # Sentence-boundary-aware chunking
    # ------------------------------------------------------------------

    def _split_into_chunks(
        self, text: str, chunk_size: int = 1000, overlap: int = 200
    ) -> List[str]:
        if not text:
            return []
        if len(text) <= chunk_size:
            return [text]

        # Split into sentences first
        sentences = re.split(r'(?<=[.!?])\s+|\n{2,}', text)
        sentences = [s.strip() for s in sentences if s.strip()]

        chunks: List[str] = []
        current_chunk: List[str] = []
        current_len = 0

        for sentence in sentences:
            sent_len = len(sentence)

            # If a single sentence exceeds chunk_size, split it by character
            if sent_len > chunk_size:
                # Flush current chunk first
                if current_chunk:
                    chunks.append(' '.join(current_chunk))
                    current_chunk = []
                    current_len = 0

                # Hard-split the long sentence
                for start in range(0, sent_len, chunk_size - overlap):
                    chunks.append(sentence[start:start + chunk_size])
                continue

            if current_len + sent_len + 1 > chunk_size and current_chunk:
                chunks.append(' '.join(current_chunk))
                # Keep last few sentences for overlap
                overlap_sentences: List[str] = []
                overlap_len = 0
                for s in reversed(current_chunk):
                    if overlap_len + len(s) > overlap:
                        break
                    overlap_sentences.insert(0, s)
                    overlap_len += len(s) + 1
                current_chunk = overlap_sentences
                current_len = sum(len(s) for s in current_chunk) + max(0, len(current_chunk) - 1)

            current_chunk.append(sentence)
            current_len += sent_len + (1 if current_len > 0 else 0)

        if current_chunk:
            chunks.append(' '.join(current_chunk))

        return chunks if chunks else [text]

    # ------------------------------------------------------------------
    # Delete by metadata
    # ------------------------------------------------------------------

    def delete_by_source(self, source: str) -> int:
        """Remove all chunks whose metadata['source'] matches *source*.
        Returns the number of removed chunks."""
        if not self.healthy:
            return 0
        try:
            removed_ids = set()
            for _lane_name, collection in self._collections_for_delete():
                results = collection.get(
                    where={"source": source},
                    include=[],
                )
                ids = results.get("ids", [])
                if ids:
                    collection.delete(ids=ids)
                    removed_ids.update(ids)
            logger.info(f"Deleted {len(removed_ids)} chunks for source={source}")
            return len(removed_ids)
        except Exception as e:
            logger.error(f"delete_by_source failed: {e}")
            return 0

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def retrieve(self, query: str, k: int = 5) -> List[str]:
        return [r['document'] for r in self.search(query, k)]

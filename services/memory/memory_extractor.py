"""
memory_extractor.py

Background auto-extraction of facts from chat conversations.
After each LLM response, this module sends the last few messages to the LLM
asking it to extract memorable facts, then stores them in both memory.json
and the FAISS vector index.

Periodically audits all memories via LLM to consolidate duplicates,
rewrite vague entries, and remove junk.
"""

import hashlib
import json
import logging
import os
import re
from typing import Optional

from src.memory import MemoryStoreUnreadable
from services.memory.extraction_context import conversation_for_extraction

logger = logging.getLogger(__name__)


def _tidy_state_path(memory_manager) -> str:
    """Sidecar JSON next to memory.json that remembers the fingerprint of
    the last successfully-audited state per owner. Lets the audit short-
    circuit when nothing has changed since the previous tidy — running
    the LLM again on an already-clean list was wasting 30-120s per call
    and occasionally timing out on the second pass."""
    return os.path.join(os.path.dirname(memory_manager.memory_file), "memory_tidy_state.json")


def _fingerprint_entries(entries) -> str:
    """Stable hash of an owner's memories — order-independent, depends
    only on id+text+category. Any add/edit/delete invalidates it."""
    items = sorted(
        (str(e.get("id", "")), e.get("text", ""), e.get("category", ""))
        for e in _memory_dicts(entries)
    )
    h = hashlib.sha256()
    for triple in items:
        h.update(("\x1f".join(triple) + "\x1e").encode("utf-8"))
    return h.hexdigest()


def _memory_dicts(entries):
    for entry in entries or []:
        if isinstance(entry, dict):
            yield entry


def _load_tidy_state(memory_manager) -> dict:
    path = _tidy_state_path(memory_manager)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_tidy_state(memory_manager, owner: Optional[str], fingerprint: str) -> None:
    path = _tidy_state_path(memory_manager)
    state = _load_tidy_state(memory_manager)
    state[owner or ""] = {"fingerprint": fingerprint}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        logger.warning(f"Could not persist tidy fingerprint: {e}")

# One category list for every extraction prompt (auto, manual, import), so a
# fact does not get a different label depending on which button produced it.
MEMORY_CATEGORIES = ("identity", "preference", "fact", "contact", "project", "goal")
_CATEGORY_LINE = "Categories: " + ", ".join(f"'{c}'" for c in MEMORY_CATEGORIES)

EXTRACT_SYSTEM_PROMPT = (
    "You are a memory extraction assistant. Analyze the conversation and extract ONLY "
    "durable personal facts about the user that would be useful across many future conversations.\n\n"
    "Good examples: name, job title, city, family members, long-term projects, strong preferences.\n"
    "Bad examples: what they asked about today, temporary moods, generic statements, "
    "things the assistant said, one-off tasks, opinions on the current topic.\n\n"
    "Rules:\n"
    "- At most 2 facts per conversation, the most important ones\n"
    "- Only extract facts the USER stated or clearly implied\n"
    "- Each fact is a single short sentence (under 15 words)\n"
    "- If nothing durable was revealed, return []\n\n"
    "Return a JSON array of objects with 'text' and 'category' fields.\n"
    f"{_CATEGORY_LINE}\n\n"
    "Return ONLY valid JSON, no markdown fences."
)

# The manual "extract from this chat" button. Same transcript framing as the
# auto extractor (see build_extraction_messages), a wider net: the user asked.
MANUAL_EXTRACT_SYSTEM_PROMPT = (
    "You are a memory extraction assistant. Extract facts the user stated that "
    "will matter in future conversations: contacts, addresses, long-term projects, "
    "preferences.\n\n"
    "Return a JSON array of objects with 'text' and 'category' fields. "
    "Return [] when there are none.\n"
    f"{_CATEGORY_LINE}\n\n"
    "Return ONLY valid JSON, no markdown fences."
)

# Messages the manual extract reads. The auto extractor looks at CONTEXT_WINDOW
# (the last turn or two); the button is a request for the whole chat, bounded so
# a long session cannot blow the context window.
MANUAL_CONTEXT_WINDOW = 40

# How many recent messages to include for extraction
CONTEXT_WINDOW = 6


def build_extraction_messages(window: list, system_prompt: str) -> list:
    """Frame a message window as one transcript to analyze, not a chat to continue.

    Passed as raw alternating chat turns, the model treats the window as a
    conversation to CONTINUE and reliably extracts nothing (0/6 trials against
    6/6 with this shape; this was the cause of auto-memory logging "0
    candidates" on every run). The manual extract used raw turns until
    2026-10-01 (audit A4-10) and silently returned nothing on reasoning models.
    """
    def _text(m):
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(
                b.get("text", "") for b in c
                if isinstance(b, dict) and b.get("type") == "text"
            )
        return f"{m.get('role', '?')}: {c}"

    transcript = "\n\n".join(_text(m) for m in window)
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            "Conversation to analyze:\n\n" + transcript
            + "\n\nReturn the JSON array of durable facts now (or [] if none)."
        )},
    ]

# 2026-10-01 audit A4-11: the audit used to return the whole cleaned list, so
# output grew with the store and a truncated reply risked losing entries. It now
# returns only the changes; an entry the model does not mention is untouched.
AUDIT_SYSTEM_PROMPT = (
    "Review the saved memories. Return a JSON object "
    '{"merge": [{"keep_id": "...", "drop_ids": ["..."], "text": "merged wording"}], '
    '"drop": [{"id": "...", "reason": "..."}]}.\n'
    "Merge only entries that state the same fact in different words ('Name is Sam' "
    "and 'Called Sam'); 'Likes Python' and 'Uses Python at work' stay separate. "
    "Keep the original wording when merging. "
    "Drop entries that describe what the assistant did, or that have no content. "
    "Use only ids from the list. When unsure, leave the entry alone. "
    "Return {\"merge\": [], \"drop\": []} when nothing needs to change.\n\n"
    "Return ONLY valid JSON, no markdown fences."
)

AUDIT_INTERVAL = 5  # audit every N new memories added, per owner
# Per owner: a shared counter let one user's extractions trigger a
# whole-corpus LLM audit of another user's memories.
_extractions_since_audit: dict = {}


def _message_text(message) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return " ".join(p for p in parts if p).strip()
    return ""


def _message_role(message) -> str:
    role = getattr(message, "role", None)
    if role is None and isinstance(message, dict):
        role = message.get("role")
    return str(role or "").lower()


def _clean_memory_value(value: str, max_len: int = 80) -> str:
    value = re.sub(r"\s+", " ", value or "").strip(" .,!?:;\"'`“”‘’")
    value = re.sub(r"^(?:the|a|an)\s+", "", value, flags=re.I)
    if not value or len(value) > max_len:
        return ""
    if re.search(r"https?://|@|[{}<>]", value):
        return ""
    return value


def _fallback_memory_candidates(messages) -> list[dict]:
    """Extract obvious durable facts without relying on the LLM.

    This is deliberately narrow. The LLM remains the main extractor, but
    simple identity/preference/goal statements should not silently vanish just
    because the background model judged them too conversational.
    """
    candidates = []
    seen = set()

    def add(text: str, category: str):
        text = _clean_memory_value(text, 120)
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append({"text": text, "category": category})

    for msg in messages:
        if _message_role(msg) != "user":
            continue
        text = _message_text(msg)
        if not text:
            continue

        m = re.search(r"\bmy name is\s+([A-Za-z][A-Za-z0-9 .'\-]{1,50})\b", text, re.I)
        if m:
            name = _clean_memory_value(m.group(1), 50)
            if name:
                add(f"User's name is {name}.", "identity")

        m = re.search(r"\bcall me\s+([A-Za-z][A-Za-z0-9 .'\-]{1,50})\b", text, re.I)
        if m:
            name = _clean_memory_value(m.group(1), 50)
            if name:
                add(f"User wants to be called {name}.", "identity")

        m = re.search(r"\bi (?:live in|am from|'m from)\s+([^.!?\n]{2,80})", text, re.I)
        if m:
            place = _clean_memory_value(m.group(1), 80)
            if place:
                add(f"User lives in {place}.", "identity")

        m = re.search(r"\bi (prefer|like|love|hate|do not like|don't like)\s+([^.!?\n]{4,100})", text, re.I)
        if m:
            preference = _clean_memory_value(m.group(2), 100)
            if preference:
                # The same pattern catches likes and dislikes; keep the stored
                # sentiment faithful instead of recording every match as a
                # preference ("I hate cilantro" must not become "User prefers
                # cilantro").
                verb = m.group(1).lower()
                if verb in ("hate", "do not like", "don't like"):
                    add(f"User dislikes {preference}.", "preference")
                else:
                    add(f"User prefers {preference}.", "preference")

        m = re.search(
            r"\bi (?:(?:want|would like|plan|hope) to|wanna) "
            r"(?:go|travel|move|visit) to\s+([^.!?\n]{2,80})",
            text,
            re.I,
        )
        if m:
            destination = _clean_memory_value(m.group(1), 80)
            if destination:
                add(f"User wants to visit {destination}.", "goal")

    return candidates[:2]


def _is_text_duplicate(new_text: str, existing: list, threshold: float = 0.6) -> bool:
    """Check if new_text is too similar to any existing memory (Jaccard similarity)."""
    new_tokens = set(new_text.lower().split())
    if not new_tokens:
        return False
    for entry in _memory_dicts(existing):
        old_tokens = set(entry.get("text", "").lower().split())
        if not old_tokens:
            continue
        intersection = new_tokens & old_tokens
        union = new_tokens | old_tokens
        if len(intersection) / len(union) >= threshold:
            return True
    return False


def _parse_extraction_json(raw: str) -> list:
    """Parse the extraction LLM's reply into a list of facts, tolerating
    reasoning-model noise.

    The model emits <think>…</think> (and sometimes a prose preamble or a
    ```json fence) AROUND the JSON array; without stripping it, json.loads
    bombs and the run silently yields "0 candidates". Pure str -> list (no
    LLM/network); returns [] on any parse failure instead of raising.
    """
    text = (raw or "").strip()
    try:
        from src.text_helpers import strip_think as _strip_think
        text = _strip_think(text, prose=True, prompt_echo=True).strip()
    except Exception:
        pass
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    # JSON may still be embedded in surrounding commentary (leading prose or
    # trailing remarks like "[...] Done!") — slice from the first '[' to the
    # last ']' whenever both exist. Slice unconditionally: a reply that starts
    # with '[' can still carry trailing commentary that breaks json.loads.
    _start = text.find("[")
    _end = text.rfind("]")
    if 0 <= _start < _end:
        text = text[_start : _end + 1]

    try:
        facts = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("Memory extraction returned non-JSON: %r", (raw or "")[:120])
        return []
    except Exception:
        logger.debug("Memory extraction returned non-JSON: %r", (raw or "")[:120])
        return []
    return facts if isinstance(facts, list) else []


async def extract_and_store(
    session,
    memory_manager,
    memory_vector,
    endpoint_url: str,
    model: str,
    headers: Optional[dict] = None,
    *,
    llm_candidates: Optional[list] = None,
):
    """Extract facts from recent conversation and store them.

    Designed to run as a background task (asyncio.create_task).
    Errors are logged, never raised.
    """
    if not endpoint_url or not model:
        logger.debug("[memory-extract] No model or URL provided, skipping")
        return

    try:
        from src.llm_core import llm_call_async, llm_call_async_with_fallback

        # Get last N messages from session
        recent = conversation_for_extraction(session.get_context_messages(), limit=CONTEXT_WINDOW)

        if len(recent) < 2 or not any(m["role"] == "user" for m in recent):
            return  # Need at least a user message and assistant response

        # Strip media (images/audio) from messages — background memory extraction
        # only needs the text. The VL-generated descriptions are already in the
        # text content of the messages. This avoids sending image tokens to
        # non-vision models and prevents accidental "vision grounding" triggers.
        stripped_recent = []
        for msg in recent:
            role = msg.get("role")
            content = msg.get("content", "")
            if isinstance(content, list):
                # Filter out multimodal blocks that aren't text
                text_only = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
                if not text_only and content:
                    continue
                content = text_only
            stripped_recent.append({"role": role, "content": content})

        if not stripped_recent:
            return

        fallback_facts = _fallback_memory_candidates(stripped_recent)
        llm_ran = False

        # One transcript-to-analyze message, not raw chat turns (see
        # build_extraction_messages for why).
        extraction_messages = build_extraction_messages(stripped_recent, EXTRACT_SYSTEM_PROMPT)

        facts = []
        try:
            call_kwargs = {
                "temperature": 0.1,
                # A reasoning model spends most of its budget on <think> tokens
                # BEFORE emitting the JSON, so the old 500 truncated the response
                # before any JSON appeared → every run logged "0 candidates". The
                # audit path hit the same wall and raised to 16384; extraction's
                # output (a short facts list) is small, so an ample ceiling is
                # enough once thinking has room.
                "max_tokens": 4096,
                "workload": "background",
            }
            if llm_candidates:
                raw = await llm_call_async_with_fallback(
                    llm_candidates,
                    extraction_messages,
                    **call_kwargs,
                )
            else:
                raw = await llm_call_async(
                    endpoint_url,
                    model,
                    extraction_messages,
                    headers=headers,
                    **call_kwargs,
                )

            # Parse JSON, tolerating reasoning-model noise (<think> blocks, a
            # ```json fence, and leading/trailing commentary). See
            # _parse_extraction_json — returns [] rather than raising.
            facts = _parse_extraction_json(raw)
            llm_ran = True
        except Exception as e:
            logger.warning(f"LLM memory extraction failed; using fallback candidates if available: {e}")

        if not isinstance(facts, list):
            facts = []

        if fallback_facts:
            if llm_ran:
                # The model saw the same window and judged it. Only let the
                # regex add identity facts it might have skipped ("my name
                # is …"); a matched "I like X" in a brainstorm is not a
                # durable preference (a naming chat produced "User prefers
                # Umni" this way).
                fallback_facts = [f for f in fallback_facts if f.get("category") == "identity"]
            facts = list(facts) + fallback_facts

        if not facts:
            logger.info("Auto memory extraction ran: 0 candidates")
            return

        # Get owner from session
        _owner = getattr(session, 'owner', None)

        # Strict load: this is a read-modify-write. Degrading to [] here would
        # save only the newly extracted facts and drop the entire store.
        try:
            existing = memory_manager.load_all_for_update()
        except MemoryStoreUnreadable as e:
            logger.error("Skipping auto memory extraction, store unreadable: %s", e)
            return
        added = 0

        for fact in facts:
            if isinstance(fact, str):
                fact_text = fact
                category = "fact"
            elif isinstance(fact, dict):
                fact_text = fact.get("text", "").strip()
                category = fact.get("category", "fact")
            else:
                continue

            if not fact_text or len(fact_text) < 5:
                continue

            # Dedup: check vector similarity first (fast), then exact text match.
            # A runtime embedding/ChromaDB failure (backend OOM, model evicted,
            # remote endpoint down) must not abort the whole batch — fall through
            # to the text/fuzzy dedup below instead of losing every validated
            # fact extracted this session. (`.healthy` is only set at init, so
            # it does not catch failures that develop later.)
            if memory_vector and memory_vector.healthy:
                try:
                    existing_id = memory_vector.find_similar(fact_text, threshold=0.72)
                except Exception as e:
                    logger.warning(f"Memory dedup (vector) unavailable, using text fallback: {e}")
                    existing_id = None
                if existing_id:
                    # The vector store is a single shared collection with no
                    # owner metadata, so find_similar can return ANOTHER
                    # tenant's memory. Only treat it as a duplicate when the
                    # match is this user's own (or a legacy unowned) memory —
                    # otherwise the user's freshly-extracted fact would be
                    # silently dropped. Mirror the owner predicate used by the
                    # text dedup below; cross-tenant/stale matches fall through.
                    _match = next((e for e in existing if e.get("id") == existing_id), None)
                    if _match is not None and (_match.get("owner") == _owner or _match.get("owner") is None):
                        logger.debug(f"Memory dedup (vector): '{fact_text[:50]}' matches {existing_id}")
                        continue

            # Text dedup fallback: exact match + fuzzy similarity
            user_existing = [e for e in existing if e.get("owner") == _owner or e.get("owner") is None] if _owner else existing
            if memory_manager.find_duplicates(fact_text, user_existing):
                continue
            # Fuzzy text similarity check (catches rephrased duplicates when vector index is unavailable)
            if _is_text_duplicate(fact_text, user_existing):
                logger.debug(f"Memory dedup (fuzzy): '{fact_text[:50]}' too similar to existing")
                continue

            entry = memory_manager.add_entry(fact_text, source="auto", category=category, owner=_owner)
            # Auto-pin identity facts (name, job, location) — core context
            if category == "identity":
                entry["pinned"] = True
            if hasattr(session, "session_id"):
                entry["session_id"] = session.session_id
            elif hasattr(session, "name"):
                entry["session_id"] = session.name

            existing.append(entry)

            # Add to vector index. The JSON store (saved below) is the source of
            # truth and the keyword path can still retrieve this entry, so a vector
            # write failure must not drop the fact or abort the remaining batch.
            if memory_vector and memory_vector.healthy:
                try:
                    memory_vector.add(entry["id"], fact_text)
                except Exception as e:
                    logger.warning(f"Memory vector add failed for {entry['id']}: {e}")

            added += 1

        if added > 0:
            memory_manager.save(existing)
            try:
                from src.event_bus import fire_event
                for _ in range(added):
                    fire_event("memory_added", _owner)
            except Exception:
                logger.debug("memory_added event dispatch failed", exc_info=True)
            logger.info(f"Auto-extracted {added} memories from session")

            _audit_key = _owner or ""
            _extractions_since_audit[_audit_key] = _extractions_since_audit.get(_audit_key, 0) + added
            if _extractions_since_audit[_audit_key] >= AUDIT_INTERVAL:
                _extractions_since_audit[_audit_key] = 0
                logger.info("Audit threshold reached, running memory audit")
                await audit_memories(
                    memory_manager, memory_vector, endpoint_url, model, headers, owner=_owner
                )
        else:
            logger.info("Auto memory extraction ran: 0 added")

    except Exception as e:
        logger.error(f"Memory extraction failed: {e}")


def _parse_audit_changes(raw: str) -> Optional[dict]:
    """Parse the audit reply into {"merge": [...], "drop": [...]}, or None.

    Accepts the first JSON object in the reply. A bare list is the old
    return-everything format and is refused: applying it as changes would read
    every entry as a request.
    """
    text = re.sub(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", raw or "", flags=re.I).strip()
    decoder = json.JSONDecoder()
    pos = text.find("{")
    while pos != -1:
        for candidate in (text[pos:], re.sub(r",(\s*[}\]])", r"\1", text[pos:])):
            try:
                value, _ = decoder.raw_decode(candidate)
            except ValueError:
                continue
            if isinstance(value, dict) and ("merge" in value or "drop" in value):
                merge = value.get("merge") or []
                drop = value.get("drop") or []
                if isinstance(merge, list) and isinstance(drop, list):
                    return {"merge": merge, "drop": drop}
            break
        pos = text.find("{", pos + 1)
    return None


def _apply_audit_changes(existing: list, changes: dict) -> list:
    """Apply merge/drop changes to an owner's entries and return the survivors.

    Only ids that exist are acted on, an entry named in "merge" as keep_id is
    never dropped, and pinned entries are never removed (a pinned identity fact
    is core context). Anything malformed is skipped, which leaves the entry as
    it was.
    """
    by_id = {m["id"]: m for m in _memory_dicts(existing)}
    texts: dict = {}
    removed: set = set()
    keepers: set = set()

    for item in changes.get("merge", []):
        if not isinstance(item, dict):
            continue
        keep_id = item.get("keep_id")
        if keep_id not in by_id:
            continue
        keepers.add(keep_id)
        new_text = item.get("text")
        if isinstance(new_text, str) and new_text.strip():
            texts[keep_id] = new_text.strip()
        for drop_id in item.get("drop_ids") or []:
            if isinstance(drop_id, str) and drop_id in by_id and drop_id != keep_id:
                removed.add(drop_id)

    for item in changes.get("drop", []):
        drop_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(drop_id, str) and drop_id in by_id:
            removed.add(drop_id)

    removed -= keepers
    removed = {i for i in removed if not by_id[i].get("pinned")}

    final = []
    for m in _memory_dicts(existing):
        if m["id"] in removed:
            continue
        entry = m.copy()
        if m["id"] in texts:
            entry["text"] = texts[m["id"]]
        final.append(entry)
    return final


async def audit_memories(
    memory_manager,
    memory_vector,
    endpoint_url: str,
    model: str,
    headers: Optional[dict] = None,
    owner: Optional[str] = None,
):
    """Send all memories to the LLM for deduplication and consolidation.

    - Merges near-duplicate entries
    - Rewrites vague entries to be concise
    - Removes junk / non-personal entries
    - Rebuilds the vector index afterwards

    Safe to call manually or from the automatic trigger in extract_and_store.
    Errors are logged, never raised.
    """
    try:
        from src.llm_core import llm_call_async

        existing = memory_manager.load(owner=owner)
        if not existing:
            logger.info("Memory audit: nothing to audit")
            return {"before": 0, "after": 0}

        before_count = len(existing)

        # Skip the LLM call entirely when this exact set of memories was
        # already audited — the previous tidy left them in a clean state
        # and nothing has changed since. Returns instantly so the UI shows
        # "Already clean" without spending 30-120s on a wasted LLM round.
        # The fingerprint includes id+text+category; any add/edit/delete
        # invalidates it and the audit runs normally.
        current_fp = _fingerprint_entries(existing)
        last_state = _load_tidy_state(memory_manager).get(owner or "") or {}
        if last_state.get("fingerprint") == current_fp:
            logger.info("Memory audit: state unchanged since last tidy — skipping LLM")
            return {
                "before": before_count,
                "after": before_count,
                "already_tidy": True,
            }

        # Build payload: list of {id, text, category} for the LLM
        memory_payload = [
            {"id": m["id"], "text": m["text"], "category": m.get("category", "fact")}
            for m in existing
        ]

        audit_messages = [
            {"role": "system", "content": AUDIT_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(memory_payload, ensure_ascii=False)},
        ]

        raw = await llm_call_async(
            endpoint_url,
            model,
            audit_messages,
            temperature=0.1,
            # 16384 (was 2000): the deduped list of all memories can be large,
            # and a reasoning model spends tokens thinking first — 2000 truncated
            # the JSON so it never parsed ("bad_json").
            max_tokens=16384,
            headers=headers,
            # Bound the call so the Tidy whirlpool can't spin indefinitely on a
            # slow/large generation.
            timeout=120,
        )

        # Parse the JSON object, tolerating reasoning-model noise: <think>
        # blocks, markdown fences, leading prose, and trailing commas.
        changes = _parse_audit_changes(raw)
        if changes is None:
            logger.error(f"Memory audit returned an unusable reply: {(raw or '')[:300]}")
            return {"before": before_count, "after": before_count, "error": "bad_json"}

        final_entries = _apply_audit_changes(existing, changes)
        after_count = len(final_entries)

        # Safety net against catastrophic over-deletion. A conservative tidy
        # should never wipe out half the store in one pass — if the model
        # returned far fewer entries than it was given (over-consolidation, a
        # dropped/truncated list, or it ignored ids), treat it as a misfire and
        # DON'T save. Better to no-op than to silently lose memories.
        if before_count >= 8 and after_count < before_count * 0.5:
            logger.warning(
                f"Memory audit would cut {before_count} -> {after_count} "
                f"(>50% removed) — refusing as unsafe, keeping originals"
            )
            return {"before": before_count, "after": before_count, "error": "unsafe_removal"}

        if final_entries == list(existing):
            # Nothing the model proposed was applicable: no save, no re-embed.
            _save_tidy_state(memory_manager, owner, current_fp)
            return {"before": before_count, "after": before_count}

        # Merge audited entries back with other users' entries
        if owner:
            # Strict load: the merge below reconstructs the whole file. If this
            # degraded to [] we would save only this owner's audited slice and
            # destroy every other tenant's memories.
            try:
                all_entries = memory_manager.load_all_for_update()
            except MemoryStoreUnreadable as e:
                logger.error("Aborting memory audit save, store unreadable: %s", e)
                return {
                    "before": before_count,
                    "after": before_count,
                    "error": "store_unreadable",
                }
            audited_ids = {e["id"] for e in final_entries}
            other_entries = [e for e in all_entries if e.get("owner") != owner and (e.get("owner") is not None)]
            # Also keep legacy entries that weren't part of this audit
            for e in all_entries:
                if e.get("owner") is None and e["id"] not in audited_ids and e["id"] not in {o["id"] for o in other_entries}:
                    other_entries.append(e)
            saved_entries = final_entries + other_entries
        else:
            saved_entries = final_entries
        memory_manager.save(saved_entries)
        logger.info(
            f"Memory audit complete: {before_count} -> {after_count} entries "
            f"({before_count - after_count} removed/merged)"
        )

        # Reconcile the vector index with the full saved set (all owners, so
        # the shared collection keeps every tenant's entries). ``sync`` deletes
        # and re-embeds only what changed; the old full ``rebuild`` dropped the
        # collection and re-embedded every entry, leaving concurrent chat
        # turns with an empty memory search meanwhile.
        if memory_vector and memory_vector.healthy:
            sync = getattr(memory_vector, "sync", None)
            if callable(sync):
                sync(saved_entries)
            else:
                memory_vector.rebuild(saved_entries)

        # Persist the post-tidy fingerprint so the next call short-circuits
        # if nothing has changed in the meantime.
        _save_tidy_state(memory_manager, owner, _fingerprint_entries(final_entries))

        return {"before": before_count, "after": after_count}

    except Exception as e:
        logger.error(f"Memory audit failed: {e}")
        return {"error": str(e)}

"""Pins for the 2026-09-10 harness efficiency specs.

Spec 1 — stable prompt prefix on the Responses path: mid-turn runtime notes
are delivered at the tail (user role) instead of being hoisted into the
instructions block; replayed reasoning is pruned in batches; the payload
carries a per-conversation prompt_cache_key.

Spec 2 — tool-schema hygiene: admin tools are added per matched keyword, not
as a blanket set; low-signal turns skip embedding retrieval.

Spec 3 — memory/vault/email hygiene: the vector index is reconciled in place
instead of dropped, the audit counter is per owner, the regex fallback only
adds identity facts once the model has judged the window, and promotional /
list mail never reaches calendar extraction.
"""
import pytest

from src import agent_loop
from src.agent_loop import _detect_admin_tools, _harness_directive, _tool_schemas_for_round, _ADMIN_TOOLS
from src.llm_core import _build_chatgpt_responses_payload
from src.tool_index import ToolIndex, ALWAYS_AVAILABLE


# ── Spec 1 ──

def test_harness_directive_is_a_tail_user_message_not_system():
    msg = _harness_directive("STOP calling tools and answer.")
    assert msg["role"] == "user"
    assert msg["content"].startswith("[Harness directive")
    assert "STOP calling tools" in msg["content"]


def test_no_mid_turn_system_appends_remain_in_the_round_loop():
    """llm_core hoists every role=system message into the instructions
    prefix, so a mid-turn system append invalidates the cache for the whole
    conversation. The round loop must use _harness_directive instead."""
    import inspect
    src = inspect.getsource(agent_loop.stream_agent_loop)
    assert 'messages.append({\n                        "role": "system"' not in src
    assert 'messages.append({\n                    "role": "system"' not in src
    assert 'messages.append({\n                "role": "system"' not in src
    assert src.count("_harness_directive(") >= 5


def test_prompt_cache_key_rides_on_the_responses_payload(monkeypatch):
    payload = _build_chatgpt_responses_payload(
        "gpt-5.6-luna", [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        0.7, 100, stream=True, cache_key="session-abc",
    )
    assert payload["prompt_cache_key"] == "session-abc"
    assert payload["store"] is False
    none = _build_chatgpt_responses_payload("gpt-5.6-luna", [{"role": "user", "content": "hi"}], 0.7, 100)
    assert "prompt_cache_key" not in none
    monkeypatch.setattr("src.llm_core._responses_prompt_cache_key_enabled", lambda: False)
    off = _build_chatgpt_responses_payload("gpt-5.6-luna", [{"role": "user", "content": "hi"}], 0.7, 100, cache_key="s")
    assert "prompt_cache_key" not in off


# ── Spec 2 ──

def _user(text):
    return [{"role": "user", "content": text}]


def test_admin_tools_follow_the_matched_keyword_only():
    assert _detect_admin_tools(_user("add a task to remind me tomorrow")) == {"manage_tasks"}
    assert _detect_admin_tools(_user("delete this chat")) >= {"manage_session"}
    assert "manage_webhooks" not in _detect_admin_tools(_user("delete this chat"))
    assert _detect_admin_tools(_user("show my settings")) == {"manage_settings"}
    assert _detect_admin_tools(_user("what's the weather like")) == set()
    # "admin" itself still means the whole management surface.
    assert _detect_admin_tools(_user("open the admin panel")) == set(_ADMIN_TOOLS)


def test_schema_builder_ships_only_the_matched_admin_tools():
    kwargs = dict(force_answer=False, is_api_model=True, relevant_tools={"read_file"}, needs_admin=True,
                  mcp_schemas=[], disabled_tools=set(), ody_qwen_finetune_model=False, last_user="")
    names = {s["function"]["name"] for s in _tool_schemas_for_round(admin_tools={"manage_tasks"}, **kwargs)}
    assert names == {"read_file", "manage_tasks"}
    # No breakdown supplied: the old blanket behaviour is the fallback.
    fallback = {s["function"]["name"] for s in _tool_schemas_for_round(**kwargs)}
    assert _ADMIN_TOOLS <= fallback
    # An empty matched set means no admin schemas at all.
    none = {s["function"]["name"] for s in _tool_schemas_for_round(admin_tools=set(), **kwargs)}
    assert none == {"read_file"}


def test_low_signal_selection_skips_embedding_retrieval():
    ti = ToolIndex.__new__(ToolIndex)

    def boom(query, k=8):
        raise AssertionError("embedding retrieval must not run for low-signal turns")

    ti.retrieve = boom
    assert ti.get_tools_for_query("i like Umni", use_embeddings=False) == set(ALWAYS_AVAILABLE)
    # Hints still apply without embeddings.
    assert "manage_calendar" in ti.get_tools_for_query("add a calendar event", use_embeddings=False)


# ── Spec 3 ──

class _Collection:
    def __init__(self, docs):
        self.docs = dict(docs)
        self.deleted = []
        self.added = []

    def get(self, ids=None, include=None):
        if ids is not None:
            found = [i for i in ids if i in self.docs]
            return {"ids": found, "documents": [self.docs[i] for i in found]}
        return {"ids": list(self.docs), "documents": list(self.docs.values())}

    def delete(self, ids):
        self.deleted.extend(ids)
        for i in ids:
            self.docs.pop(i, None)

    def add(self, ids, embeddings, documents, metadatas):
        self.added.extend(ids)
        for i, d in zip(ids, documents):
            self.docs[i] = d


class _Lane:
    name = "fastembed"

    def __init__(self, docs):
        self.collection = _Collection(docs)
        self.encoded = 0

    def encode(self, texts):
        self.encoded += len(texts)
        return [[0.0] * 4 for _ in texts]


def test_memory_vector_sync_reconciles_in_place_without_dropping_the_collection():
    from src.memory_vector import MemoryVectorStore

    store = MemoryVectorStore.__new__(MemoryVectorStore)
    store._healthy = True
    lane = _Lane({"a": "User likes tea", "b": "User lives in Oslo", "c": "stale junk"})
    store._lanes = [lane]
    stats = store.sync([
        {"id": "a", "text": "User likes tea"},          # unchanged
        {"id": "b", "text": "User lives in Bergen"},    # text changed
        {"id": "d", "text": "User's name is Robert"},   # new
    ])
    assert stats == {"added": 1, "updated": 1, "removed": 1}
    assert set(lane.collection.docs) == {"a", "b", "d"}
    assert lane.collection.docs["b"] == "User lives in Bergen"
    assert lane.encoded == 2  # only the changed and the new entry were re-embedded
    assert "a" not in lane.collection.deleted


@pytest.mark.asyncio
async def test_audit_syncs_instead_of_rebuilding(monkeypatch):
    from services.memory import memory_extractor as me

    calls = {}

    class Vec:
        healthy = True

        def sync(self, entries):
            calls["sync"] = len(entries)
            return {}

        def rebuild(self, entries):
            calls["rebuild"] = len(entries)

    class Mgr:
        def load(self, owner=None):
            return [{"id": "1", "text": "User likes tea", "category": "preference", "owner": "alice"},
                    {"id": "2", "text": "User likes tea a lot", "category": "preference", "owner": "alice"}]

        def load_all_for_update(self):
            return self.load()

        def save(self, entries):
            calls["saved"] = entries

    async def fake_llm(*args, **kwargs):
        return '[{"id": "1", "text": "User likes tea", "category": "preference"}]'

    monkeypatch.setattr("src.llm_core.llm_call_async", fake_llm)
    monkeypatch.setattr(me, "_load_tidy_state", lambda m: {})
    monkeypatch.setattr(me, "_save_tidy_state", lambda *a, **k: None)
    out = await me.audit_memories(Mgr(), Vec(), "http://x", "m", owner="alice")
    assert out == {"before": 2, "after": 1}
    assert calls["sync"] == 1 and "rebuild" not in calls


def test_audit_counter_is_per_owner():
    from services.memory import memory_extractor as me

    assert isinstance(me._extractions_since_audit, dict)


def test_fallback_source_is_gated_in_extractor_source():
    """When the model ran and judged the window, only identity facts from the
    regex fallback survive (no more 'User prefers Umni' from a brainstorm)."""
    import inspect
    from services.memory import memory_extractor as me

    src = inspect.getsource(me.extract_and_store)
    assert "llm_ran" in src
    assert 'f.get("category") == "identity"' in src


def test_promotional_and_list_mail_is_not_calendar_material():
    from routes.email_pollers import _not_calendar_material

    promo = {"List-Unsubscribe": "<mailto:x>"}
    assert _not_calendar_material(promo, "news@gfuel.com", "Up to 50% Off Ends TONIGHT")
    assert _not_calendar_material({}, "hello@brand.com", "Flash sale: 30% off ends tonight")
    assert _not_calendar_material({}, "no-reply@shop.com", "Your order")
    assert not _not_calendar_material({}, "dentist@clinic.com", "Appointment confirmation for Tuesday 10am")
    assert not _not_calendar_material({}, "matt@example.com", "Coffee next week?")


# ── Skill-declared toolsets must be real tool names ──

def test_skill_requires_toolsets_keeps_only_real_tool_names():
    """A skill's `requires_toolsets` is operator-authored free text. Prose
    entries ("email", "file search and edit", "todoist") used to land in the
    selected set, where they can never resolve to a schema — nine of them
    appeared in `selected_without_schema` on every round of the 2026-09-10
    logs — and the system prompt, built from the same set, told the model it
    had tools that do not exist."""
    from src.agent_loop import _skill_declared_tools

    skills = [
        {"name": "jarvis", "requires_toolsets": [
            "email", "calendar", "todoist", "file search and edit",
            "application-log access", "memory management", "skill management",
            "read_file", "manage_calendar",
        ]},
        {"name": "other", "requires_toolsets": ["bash", "write_file"]},
    ]
    tools, unknown = _skill_declared_tools(skills, disabled_tools=set())
    assert tools == {"read_file", "manage_calendar", "bash", "write_file"}
    assert "email" in unknown and "file search and edit" in unknown
    assert "read_file" not in unknown


def test_skill_requires_toolsets_still_respects_disabled_tools():
    from src.agent_loop import _skill_declared_tools

    tools, unknown = _skill_declared_tools(
        [{"requires_toolsets": ["read_file", "bash"]}], disabled_tools={"bash"}
    )
    assert tools == {"read_file"}
    assert unknown == set()


def test_skill_requires_toolsets_handles_empty_input():
    from src.agent_loop import _skill_declared_tools

    assert _skill_declared_tools([], set()) == (set(), set())
    assert _skill_declared_tools([{"name": "x"}], set()) == (set(), set())


def test_worktree_status_names_the_checkout_it_manages():
    """The agent could not tell that a worktree it just started belongs to
    Odysseus and not to the third-party project it was working in."""
    import inspect
    from src.agent_worktree import service

    src = inspect.getsource(service.status)
    assert '"source_repo": cfg.source_repo' in src

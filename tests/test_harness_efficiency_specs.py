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
import json

import pytest

from src import agent_loop, agent_profiles
from src.agent_loop import (
    _classify_agent_request,
    _delegation_gated_tools,
    _detect_admin_tools,
    _explain_dropped_matches,
    _explicit_delegation_requested,
    _harness_directive,
    _pinned_policy_toolset,
    _reassert_pinned_toolset,
    _tool_schemas_for_round,
    apply_terminus_toolset,
    document_tools_to_drop,
    _ADMIN_TOOLS,
    _DELEGATION_TOOLS,
)
from src.llm_core import _build_chatgpt_responses_payload
from src.tool_index import ToolIndex, ALWAYS_AVAILABLE
from src.tool_policy import known_tool_names
from src.tool_security import BUILTIN_EMAIL_TOOLS


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


# Orchestration routing. A user who says, in plain language, to start another
# agent must reach the delegation tools. Two independent gates used to refuse
# the same sentence: admin keywords had no orchestration entry, and the
# `explicit` delegation policy disabled every delegation tool because its
# recogniser only knew hand-off wordings.

ORCHESTRATION_REQUESTS = [
    "ok just kick off a claude agent then and have it do it give it the logs and scope",
    "delegate this to claude code",
    "spin up a worker agent to fix the tool routing",
    "hand this to a worker",
    "run a sub-agent on this",
    "launch an agent to do the migration",
    "Launch two independent read-only source audits using agents",
    "Launch two source audits **using agents**",
]

AGENT_PROSE = [
    "why did the agent stop responding mid-answer",
    "the worker process died again overnight",
    "set the user agent header on that request",
    "how many workers does the pool start with",
    "what's the weather like",
    "explain the risks of using agents for source audits",
]


@pytest.mark.parametrize("text", ORCHESTRATION_REQUESTS)
def test_orchestration_requests_select_delegation_tools(text):
    selected = _detect_admin_tools(_user(text))
    assert selected & {"delegate_to_agent", "delegate_to_claude_code"}, selected


def test_agent_loadout_request_selects_the_loadout_tool():
    assert "manage_agent_loadout" in _detect_admin_tools(_user("show me the agent loadout"))
    assert "manage_agent_loadout" in _detect_admin_tools(_user("set up a worker to review PRs"))


@pytest.mark.parametrize("text", AGENT_PROSE)
def test_agent_prose_does_not_widen_admin_intent(text):
    """The keyword additions must not turn ordinary talk about the running
    harness into a management turn -- that is the token bloat Spec 2 exists to
    stop, and `agent`/`worker` are among the most common words here."""
    assert _detect_admin_tools(_user(text)) == set()


@pytest.mark.parametrize("text", ORCHESTRATION_REQUESTS)
def test_orchestration_requests_pass_the_explicit_delegation_gate(text):
    """`delegation_policy=explicit` (the default) disables _DELEGATION_TOOLS
    unless the human asked for a hand-off, so this predicate decides whether
    the tools admin routing just selected survive to the schema list."""
    assert _explicit_delegation_requested(text) is True


@pytest.mark.parametrize("text", AGENT_PROSE)
def test_ordinary_prose_still_keeps_delegation_off(text):
    assert _explicit_delegation_requested(text) is False


def test_selected_delegation_tools_reach_the_round_schemas():
    """End of the pipeline: what admin routing picked for the incident phrase
    is what the model is actually offered."""
    text = ORCHESTRATION_REQUESTS[0]
    admin = _detect_admin_tools(_user(text))
    disabled = set() if _explicit_delegation_requested(text) else set(_DELEGATION_TOOLS)
    names = {
        s["function"]["name"]
        for s in _tool_schemas_for_round(
            force_answer=False, is_api_model=True, relevant_tools={"read_app_logs"},
            needs_admin=True, mcp_schemas=[], disabled_tools=disabled,
            ody_qwen_finetune_model=False, last_user=text, admin_tools=admin,
        )
    }
    assert {"delegate_to_agent", "delegate_to_claude_code", "read_app_logs"} <= names


def test_dropped_query_matches_name_the_gate_that_dropped_them():
    """A retrieved tool that selection discards must say why. The delegation
    incident logged only `query_matched_count=3 selected_count=8`."""
    explained = dict(_explain_dropped_matches(
        {"delegate_to_agent", "delegate_to_claude_code", "read_app_logs"},
        {"read_app_logs"},
        {"delegate_to_agent": "delegation-policy:explicit",
         "delegate_to_claude_code": "delegation-policy:explicit"},
        set(),
    ))
    assert explained == {
        "delegate_to_agent": "delegation-policy:explicit",
        "delegate_to_claude_code": "delegation-policy:explicit",
    }
    # No gate claimed it, but it is off for this turn -> generic; neither ->
    # it was simply not carried forward, which is a different bug class.
    assert dict(_explain_dropped_matches({"a", "b"}, set(), {}, {"a"})) == {
        "a": "disabled", "b": "deselected",
    }
    # Nothing dropped, nothing logged.
    assert _explain_dropped_matches({"x"}, {"x"}, {}, set()) == []


def test_dropped_query_matches_stay_bounded():
    many = {f"tool_{i}" for i in range(30)}
    explained = _explain_dropped_matches(many, set(), {}, set(), limit=4)
    assert len(explained) == 5
    assert explained[-1] == ("+26 more", "truncated")


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
    assert tools == {
        "list_email_accounts", "list_emails", "read_email", "manage_calendar",
        "read_file", "grep", "glob", "ls", "edit_file", "write_file", "apply_patch",
        "read_app_logs", "manage_memory", "manage_skills", "bash",
    }
    # Friendly prose aliases expand to real schemas; only an unavailable
    # integration alias remains unknown.
    assert unknown == {"todoist"}
    assert not ({"email", "file search and edit", "application-log access"} & tools)


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


# ── get_workspace answers "where is the code?" in one round ──

@pytest.fixture
def checkout_roots(tmp_path, monkeypatch):
    """One checkout and one linked worktree under the approved roots."""
    from src.agent_tools import claude_code_tools as cct

    dev, wt = tmp_path / "development", tmp_path / "agent_worktrees"
    main = dev / "odysseus-main"
    (main / ".git").mkdir(parents=True)
    (main / ".git" / "HEAD").write_text("ref: refs/heads/dev\n", encoding="utf-8")
    gitdir = main / ".git" / "worktrees" / "feature"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/agent/odysseus/feature\n", encoding="utf-8")
    linked = wt / "feature"
    linked.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    monkeypatch.setattr(cct, "DEFAULT_ROOTS", (str(dev), str(wt)))
    return {"main": main, "linked": linked}


def test_get_workspace_lists_the_known_checkouts(checkout_roots, monkeypatch):
    """Two or three rounds per coding turn went to `ls /app`, `find -name
    .git` and `git -C /app status` (exit 128) before any work started."""
    import asyncio

    from src.agent_tools.filesystem_tools import GetWorkspaceTool

    monkeypatch.setattr("src.tool_execution.get_active_workspace", lambda: None)
    out = asyncio.run(GetWorkspaceTool().execute("", {}))
    assert out["exit_code"] == 0
    assert str(checkout_roots["main"]) in out["output"]
    assert "(dev)" in out["output"]
    assert str(checkout_roots["linked"]) in out["output"]
    assert "/app) is NOT a checkout" in out["output"]


def test_get_workspace_still_leads_with_the_active_workspace(checkout_roots, monkeypatch):
    import asyncio

    from src.agent_tools.filesystem_tools import GetWorkspaceTool

    monkeypatch.setattr("src.tool_execution.get_active_workspace", lambda: "/work/here")
    out = asyncio.run(GetWorkspaceTool().execute("", {}))
    assert out["output"].startswith("/work/here")
    assert "confined to this folder" in out["output"]
    assert str(checkout_roots["main"]) in out["output"]


# ── Spec 2b: every schema sent but not selected must be explainable ────────
#
# The Sept 15-16 self-audit measured 54-70 tools and 12,500-13,900 schema
# tokens per round, with one request selecting 31 tools and sending 70; the
# named surplus was Firecrawl, Penpot, notification and Context7 schemas.
# Most of that has since been closed from the other end — `mcp_manager` now
# demotes an oversized server (firecrawl: 27 tools) out of the always-bound
# set, and admin tools follow the matched keyword instead of arriving as a
# blanket seventeen.
#
# What is left is deliberate, and this pins it so nobody has to re-measure by
# hand to find out. Re-measured on the 2026-09-16 inventory, an eleven-tool
# turn is sent 21 schemas: the eleven, plus the four SMALL connected servers
# whose always-bound guarantee is the fix for the vanishing-tool bug (see
# `_tool_schemas_for_round`'s docstring and the budget note in mcp_manager).
# Nothing else. If this test starts failing with an extra name, that name is
# a new source of drift and needs its own justification here.

# The inventory behind the audit: one scraped-API catalog, four purpose-built
# servers, and the embedded catalogs that were always gated by identity.
_SEPT_INVENTORY = {
    "firecrawl": 27, "penpot": 5, "ntfy": 2, "context7": 2, "seqthink": 1,
    "builtin_browser": 25, "github_read": 20, "todoist": 8,
}
_SEPT_CATALOGS = ("builtin_browser", "github_read", "todoist")


def _sept_manager():
    from src.mcp_manager import McpManager

    mgr = McpManager()
    mgr._tools, mgr._connections = {}, {}
    for server_id, count in _SEPT_INVENTORY.items():
        mgr._tools[server_id] = [
            {"name": f"{server_id}_t{i}", "description": f"{server_id} {i}", "input_schema": {}}
            for i in range(count)
        ]
        mgr._connections[server_id] = {"status": "connected", "name": server_id, "identity": ""}
    return mgr


_LOG_TURN_SELECTION = {
    "read_app_logs", "ask_user", "update_plan", "manage_memory", "read_file",
    "grep", "glob", "ls", "bash", "web_search", "web_fetch",
}


def _sent_names(relevant, **overrides):
    mgr = _sept_manager()
    kwargs = dict(
        force_answer=False,
        is_api_model=True,
        relevant_tools=set(relevant),
        needs_admin=False,
        admin_tools=set(),
        # From the manager, not hand-rolled: the payload and the gating
        # decision then come from the same place they do in the loop.
        mcp_schemas=mgr.get_all_openai_schemas(),
        disabled_tools=set(),
        ody_qwen_finetune_model=False,
        last_user="",
        mcp_gated_names=mgr.gated_tool_names(),
    )
    kwargs.update(overrides)
    return {
        s.get("function", {}).get("name")
        for s in _tool_schemas_for_round(**kwargs)
    }


def test_the_only_unselected_schemas_are_the_small_connected_servers():
    sent = _sent_names(_LOG_TURN_SELECTION)
    surplus = sent - _LOG_TURN_SELECTION

    # Four small servers, ten tools. Every one of them is a server the user
    # connected on purpose that would otherwise vanish on "continue".
    assert len(surplus) == 10, sorted(surplus)
    assert {n.split("__")[1] for n in surplus} == {"penpot", "ntfy", "context7", "seqthink"}
    # The audit's biggest single line item is gone from the payload entirely.
    assert not any(n.startswith("mcp__firecrawl__") for n in sent)
    # ...as are the embedded catalogs, which were gated all along.
    for catalog in _SEPT_CATALOGS:
        assert not any(n.startswith(f"mcp__{catalog}__") for n in sent), catalog


def test_admin_intent_adds_only_the_tools_its_keywords_named():
    # The other half of the surplus, and the one that used to be seventeen
    # schemas on any turn that said "task" or "doc". Both live call sites pass
    # the keyword breakdown, so the blanket `_ADMIN_TOOLS` union in
    # `_tool_schemas_for_round` is a defensive default, not a live path.
    named = _detect_admin_tools(_user("add a task to remind me tomorrow and check my settings"))
    assert named == {"manage_tasks", "manage_settings"}

    sent = _sent_names(_LOG_TURN_SELECTION, needs_admin=True, admin_tools=named)
    builtin_surplus = {n for n in sent - _LOG_TURN_SELECTION if not n.startswith("mcp__")}
    assert builtin_surplus == named

    blanket = _sent_names(_LOG_TURN_SELECTION, needs_admin=True, admin_tools=None)
    assert len({n for n in blanket - _LOG_TURN_SELECTION if not n.startswith("mcp__")}) > 10, (
        "the blanket fallback is what the per-keyword breakdown replaced; if it "
        "ever becomes reachable again from stream_agent_loop this is the cost"
    )


def test_both_call_sites_pass_the_keyword_breakdown():
    import inspect

    src = inspect.getsource(agent_loop.stream_agent_loop)
    assert src.count("admin_tools=_admin_tools") == 2, (
        "a call site that omits admin_tools falls back to the whole "
        "_ADMIN_TOOLS set, silently, on every round of the turn"
    )


# ── Pinned toolsets for role-scoped agents ─────────────────────────────────
#
# A worker under a named loadout with `tool_access="selected"` used to run the
# whole per-turn pipeline — intent classification, embedding retrieval over the
# entire tool index, keyword hints, domain seeding — and have the profile
# filter the result afterwards. Two costs, both from the 2026-09-16 logs: the
# bound set varied per turn inside the allowlist, so one session's schema block
# walked 3846 → 4246 → 4500 → 4787 → 5418 tokens across consecutive turns with
# `cached=0` on several round-1s; and retrieval has no similarity floor, so
# "use ntfy to send a notification to odysseus" was handed 21 tools including
# the whole email suite.

MARKETING_LOADOUT = {
    "name": "marketing",
    "tool_access": "selected",
    "enabled_tools": [
        # The ambient five. A loadout has to name them like anything else.
        "ask_user", "update_plan", "manage_memory", "recall_tool_output",
        "search_documents",
        # The role's own work.
        "create_document", "update_document", "manage_documents",
        "web_search", "web_fetch", "manage_calendar", "manage_notes",
    ],
}

# Wordings that pull a selection in different directions: a plain request, a
# file-work request (the Terminus swap), the ntfy incident phrase, and a
# contentless one.
PINNED_TURNS = [
    "write the launch announcement for the new release",
    "fix the failing test in src/agent_loop.py and commit it",
    "use ntfy to send a notification to odysseus",
    "hey",
]


def _role_disabled_tools(loadout):
    """The deny set this loadout really reaches the agent loop as.

    `agent_profiles.session_patch` is the production path. It stores an
    allowlist AS an allowlist (`tool_access`/`enabled_tools`) and leaves
    `disabled_tools` holding only the loadout's extra denials, so reading that
    field alone reports a scoped role as unrestricted. `stored_disabled_tools`
    is the single reader that resolves both stored shapes, and it is what the
    chat route and `run_headless` feed the loop — so it is what the pin sees,
    and inverting on the live registry is what keeps a later-added tool out.
    """
    from src.session_settings import stored_disabled_tools

    profile = agent_profiles.validate_profiles([dict(loadout)])[0]
    return stored_disabled_tools(agent_profiles.session_patch(profile))


def _shaped_for_turn(selection, text):
    """Run the real per-turn shaping passes over a selection.

    Not a mirror of the loop: these are the shipping functions the loop calls
    between choosing a selection and sending it, and they are what a pinned set
    has to survive.
    """
    domains = _classify_agent_request(_user(text), text).get("domains") or set()
    shaped = apply_terminus_toolset(set(selection), query_matched=set(), domains=domains)
    shaped -= document_tools_to_drop(
        shaped, active_document_relevant=False, domains=domains,
    )
    return shaped


def _pinned_payload(text, pinned, disabled, reassert=True):
    """The exact bytes of the schema list one round would be sent."""
    selection = _shaped_for_turn(pinned, text)
    if reassert:
        selection = _reassert_pinned_toolset(pinned, selection)
    return json.dumps(_tool_schemas_for_round(
        force_answer=False, is_api_model=True, relevant_tools=selection,
        needs_admin=False, admin_tools=set(), mcp_schemas=[],
        disabled_tools=set(disabled), ody_qwen_finetune_model=False,
        last_user=text,
    ))


def test_a_role_allowlist_is_bound_whole_instead_of_being_reselected():
    pinned = _pinned_policy_toolset(_role_disabled_tools(MARKETING_LOADOUT))
    # The loadout's own list, plus `discover_tools`. The execution gate admits
    # that one for any non-empty `selected` allowlist so an agent can read back
    # its own bindings instead of guessing, so the inversion must not deny it
    # either -- otherwise the tool is hidden from every schema list while still
    # being callable, which is the phantom-tool failure the other way round.
    assert pinned == set(MARKETING_LOADOUT["enabled_tools"]) | {"discover_tools"}
    # The ambient tools survive: the pin neither drops one the policy allows
    # nor adds one back that the policy denies.
    assert set(ALWAYS_AVAILABLE) <= pinned


def test_a_policy_wider_than_the_threshold_keeps_per_turn_selection():
    """The pin is for a declared role, not a way to send everything."""
    assert _pinned_policy_toolset(set()) is None
    keep = set(sorted(known_tool_names())[:40])
    assert _pinned_policy_toolset(set(known_tool_names()) - keep) is None


def test_a_pinned_role_sends_byte_identical_schemas_every_turn():
    """Prefix stability is the point: the same bytes whatever the turn says."""
    disabled = _role_disabled_tools(MARKETING_LOADOUT)
    pinned = _pinned_policy_toolset(disabled)
    payloads = {_pinned_payload(t, pinned, disabled) for t in PINNED_TURNS}
    assert len(payloads) == 1, "a pinned role's schema prefix moved between turns"
    # Rounds of one turn all read the same selection, so the instability that
    # matters is between turns — and re-asserting the pin after the shaping
    # passes is what removes it. Without that step these same turns produce
    # more than one prefix.
    reshaped = {_pinned_payload(t, pinned, disabled, reassert=False) for t in PINNED_TURNS}
    assert len(reshaped) > 1


def test_a_pinned_role_does_not_drag_in_an_unrelated_domain():
    """The ntfy turn. Retrieval's keyword pass alone hands this query the whole
    email suite off the bare word "send"; the embedding neighbours behind the
    incident's `query_matched_count=21 selected_count=27 schema_tokens=4787`
    are on top of that. A pinned role never asks."""
    query = PINNED_TURNS[2]
    # The control the incident was written against -- retrieval's keyword pass
    # alone handing this query the whole email suite off the bare word "send" --
    # no longer reproduces: the keyword table was tightened upstream, and this
    # query's keyword pass now returns only the ambient tools. The embedding
    # neighbours behind `query_matched_count=21` are still there and are not
    # exercised here, so the control states the weaker fact it can still prove:
    # the email suite exists under names retrieval can reach, and is exactly
    # what this role must not be handed.
    assert BUILTIN_EMAIL_TOOLS <= set(known_tool_names())
    unpinned = ToolIndex.__new__(ToolIndex).get_tools_for_query(query, use_embeddings=False)
    assert unpinned, "the keyword pass returning nothing at all would make this vacuous"

    disabled = _role_disabled_tools(MARKETING_LOADOUT)
    pinned = _pinned_policy_toolset(disabled)
    assert not (pinned & BUILTIN_EMAIL_TOOLS)
    sent = {
        s["function"]["name"]
        for s in _tool_schemas_for_round(
            force_answer=False, is_api_model=True,
            relevant_tools=_reassert_pinned_toolset(pinned, _shaped_for_turn(pinned, query)),
            needs_admin=False, admin_tools=set(), mcp_schemas=[],
            disabled_tools=disabled, ody_qwen_finetune_model=False, last_user=query,
        )
    }
    assert not (sent & BUILTIN_EMAIL_TOOLS), sorted(sent & BUILTIN_EMAIL_TOOLS)
    # Subset, not equality: a name with no native schema, or a tool whose
    # capability is not configured on this host, is withheld by the same
    # builder for reasons that have nothing to do with the pin.
    assert sent <= set(MARKETING_LOADOUT["enabled_tools"]) | {"discover_tools"}
    assert {"create_document", "ask_user"} <= sent


# ── The delegation gate must cover everything that starts work elsewhere ────

def test_every_tool_orchestration_phrasing_routes_to_is_delegation_gated():
    """The structural hole `manage_agent_loadout` fell through.

    Admin routing sends "spin up a worker" to the tools that start one; the
    delegation policy decides whether those tools survive to the schema list. A
    tool the first knows about and the second does not is a tool the harness
    offers on exactly the turn it refused to delegate — which is what happened
    on 2026-09-16, when `manage_agent_loadout` (whose `start` action calls
    `agent_control.launch_worker`) stayed reachable while the three named
    delegation tools were correctly dropped.
    """
    for text in ORCHESTRATION_REQUESTS:
        routed = _detect_admin_tools(_user(text))
        assert routed, text
        assert routed <= _DELEGATION_TOOLS, (text, sorted(routed - _DELEGATION_TOOLS))


def test_run_the_agent_tests_cannot_reach_a_tool_that_starts_an_agent():
    """The incident turn. "run the agent tests" asks for no hand-off, so the
    default `explicit` policy closes the gate — and it must stay closed on the
    worst-case round: every known tool selected, with the blanket admin set
    unioned in on top.

    What no name list can cover is a remote-execution MCP tool; the refused
    turn called `mcp__pi_worker__run_pi_task` instead. See the note on
    `_DELEGATION_TOOLS` for why that belongs to `mcp_access`.
    """
    text = "run the agent tests"
    assert _explicit_delegation_requested(text) is False
    # "agent" alone is this app's own vocabulary, so admin routing does not
    # read this as orchestration either.
    assert _detect_admin_tools(_user(text)) == set()

    # Through the gate the loop actually applies, not a hand-rolled deny set:
    # naming a tool now re-opens it, and this turn names none.
    disabled = _delegation_gated_tools("explicit", text)
    assert disabled == set(_DELEGATION_TOOLS)
    sent = _sent_names(
        set(known_tool_names()) - disabled,
        disabled_tools=disabled, needs_admin=True, admin_tools=None,
    )
    assert not (sent & _DELEGATION_TOOLS), sorted(sent & _DELEGATION_TOOLS)
    assert "manage_agent_loadout" not in sent


def test_a_user_who_names_a_delegation_tool_by_name_gets_it():
    """The 2026-09-23 turn. `manage_agent_loadout` joined `_DELEGATION_TOOLS`
    because its `start` action launches a worker — correct — but under the
    default `explicit` policy that dropped it on every turn without
    orchestration phrasing, including the turn that named it outright. The
    separate `[tool-rag] User named tools` rescue could not help: it subtracts
    `disabled_tools`, and this gate had already put the name there.
    """
    text = "use manage_agent_loadout  to repair Penpot Product Designer preset"
    # No hand-off wording: the phrase recogniser is not what saves this.
    assert _explicit_delegation_requested(text) is False

    gated = _delegation_gated_tools("explicit", text)
    assert "manage_agent_loadout" not in gated
    # Only the tool that was named. Naming one launcher is not consent to all.
    assert gated == set(_DELEGATION_TOOLS) - {"manage_agent_loadout"}

    sent = _sent_names(set(known_tool_names()) - gated, disabled_tools=gated)
    assert "manage_agent_loadout" in sent
    assert not (sent & gated), sorted(sent & gated)

    # `never` means never: no wording in the turn re-opens it.
    assert _delegation_gated_tools("never", text) == set(_DELEGATION_TOOLS)


@pytest.mark.parametrize("text", [
    # The incident turn: no tool named at all.
    "run the agent tests",
    # A pasted log line that happens to contain the name.
    "why did this happen?\n21:45:30 [tool-rag] dropped 1 selected tool(s) the "
    "chat's policy disables: ['manage_agent_loadout']",
    "2026-09-23 21:45 manage_agent_loadout could not be attached",
    "WARNING manage_agent_loadout is not in the schema list",
    # Quoted text: someone else's instruction, reported rather than given.
    "> use manage_agent_loadout to repair the preset\n\nthat is what they asked for",
    # A fenced paste.
    "```\nmanage_agent_loadout\n```\nwhat does that line mean",
])
def test_a_passing_mention_does_not_open_the_delegation_gate(text):
    """A user typing the tool's name is an instruction; a quotation or a pasted
    log containing it is not. This is a policy gate, so the case it cannot read
    as an instruction goes in the restrictive bucket."""
    assert _delegation_gated_tools("explicit", text) == set(_DELEGATION_TOOLS)


# ── A pinned role's MCP tools ──────────────────────────────────────────────
#
# The 2026-09-23 incident. `_pinned_policy_toolset` built its universe from
# `known_tool_names()`, which is builtins by construction, so the complement it
# returned could not hold a single `mcp__` name: a Penpot role whose loadout
# declared 29 tools was pinned to the 14 builtins among them and — because the
# pin also skips retrieval — had no other route to its server on that turn. It
# spent four rounds discovering that the only job it has was impossible.

PENPOT_LOADOUT = {
    "name": "penpot-designer",
    "tool_access": "selected",
    "enabled_tools": [
        # The ambient five, named like anything else.
        "ask_user", "update_plan", "manage_memory", "recall_tool_output",
        "search_documents",
        # A whole connected server, by the wildcard that is the only way to
        # write "all of it" for runtime-generated names.
        "mcp__penpot__*",
        # Two tools of a server too large to be always-bound. A gated server's
        # schema reaches the payload only by winning selection — which under a
        # pin is the pinned set, and that is what stopped holding MCP names.
        "mcp__firecrawl__firecrawl_t0", "mcp__firecrawl__firecrawl_t1",
    ],
}

PENPOT_TOOL_NAMES = {f"mcp__penpot__penpot_t{i}" for i in range(_SEPT_INVENTORY["penpot"])}
NAMED_FIRECRAWL = {"mcp__firecrawl__firecrawl_t0", "mcp__firecrawl__firecrawl_t1"}


def _with_connected_mcp(monkeypatch):
    """Point every live-registry read at the audit inventory.

    The process singleton in `src.tool_utils` is what `tool_policy
    .connected_mcp_tool_names` reads, and therefore what both the allowlist
    inversion and the pin's universe see.
    """
    mgr = _sept_manager()
    monkeypatch.setattr("src.tool_utils._mcp_manager", mgr, raising=False)
    return mgr


def test_a_pinned_role_receives_the_mcp_tools_its_loadout_names(monkeypatch):
    mgr = _with_connected_mcp(monkeypatch)
    disabled = _role_disabled_tools(PENPOT_LOADOUT)
    offerable = {s["function"]["name"] for s in mgr.get_all_openai_schemas()}

    pinned = _pinned_policy_toolset(disabled, offerable)
    assert pinned is not None, "the role is well under the pin threshold"
    assert PENPOT_TOOL_NAMES <= pinned
    assert NAMED_FIRECRAWL <= pinned
    # Nothing the allowlist did not name: a wider pin would be its own bug.
    assert "mcp__firecrawl__firecrawl_t2" not in pinned
    assert not {n for n in pinned if n.startswith("mcp__ntfy__")}

    # End of the pipeline: the schemas one round is actually sent, after the
    # per-turn shaping passes a pin has to survive.
    query = "lay out the new landing page in penpot"
    sent = _sent_names(
        _reassert_pinned_toolset(pinned, _shaped_for_turn(pinned, query)),
        disabled_tools=disabled, last_user=query,
    )
    assert PENPOT_TOOL_NAMES <= sent
    assert NAMED_FIRECRAWL <= sent, (
        "a demoted server's tools reach the payload only through the turn's "
        "selection, which under a pin is the pinned set"
    )
    assert "mcp__firecrawl__firecrawl_t2" not in sent


def test_the_pinned_universe_reads_the_live_registry_by_default(monkeypatch):
    """No explicit schema list: the helper must still see connected MCP.

    The loop passes the offerable names so an operator-disabled tool neither
    gets advertised nor spends one of the pin's slots, but a caller that cannot
    produce them must not silently fall back to the builtins-only universe that
    caused the incident.
    """
    _with_connected_mcp(monkeypatch)
    pinned = _pinned_policy_toolset(_role_disabled_tools(PENPOT_LOADOUT))
    assert PENPOT_TOOL_NAMES <= pinned
    assert NAMED_FIRECRAWL <= pinned


def test_a_pinned_role_with_mcp_is_byte_stable_between_turns(monkeypatch):
    """Prefix stability is still the point, with MCP names in the set.

    A server connecting or disconnecting moves this payload — but it moves it
    either way (a disconnected server has no schema left to send, a newly
    connected small one is always-bound whatever selection said), so that is
    one invalidation at the event rather than per-turn churn. What must not
    happen is the payload moving with the turn's *wording*, and that is what
    this pins.
    """
    mgr = _with_connected_mcp(monkeypatch)
    disabled = _role_disabled_tools(PENPOT_LOADOUT)
    offerable = {s["function"]["name"] for s in mgr.get_all_openai_schemas()}
    pinned = _pinned_policy_toolset(disabled, offerable)
    payloads = {
        json.dumps(sorted(_sent_names(
            _reassert_pinned_toolset(pinned, _shaped_for_turn(pinned, t)),
            disabled_tools=disabled, last_user=t,
        )))
        for t in PINNED_TURNS
    }
    assert len(payloads) == 1, "a pinned role's schema prefix moved between turns"

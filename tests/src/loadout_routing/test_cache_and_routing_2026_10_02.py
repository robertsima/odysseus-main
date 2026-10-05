"""Three loop inefficiencies from the 2026-10-02 production bundle (gpt-6-sol, ChatGPT/Codex route).

1. A harness note ("[Harness note, not from the user] The publish request above
   was approved ...") was routed as the user's latest request: cookbook, notes
   and ui domains, two loadout suggestions for the note itself.
2. A mid-session `discover_tools` added a tool to a worker's declared list, and
   the next request was 0% cached on 52k tokens.
3. The first request after a pause of ten minutes or more was 0% cached; the
   Responses API's `prompt_cache_retention: "24h"` keeps a prompt warm longer.
"""
import asyncio
import collections
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import src.agent_loop as al
import src.llm_core as llm_core
from src import constants, stable_tools
from src.llm_core import _build_chatgpt_responses_payload

CHATGPT = "https://chatgpt.com/backend-api/codex/responses"
MSGS = [{"role": "user", "content": "hi"}]


# ── 1. harness notes route as a continuation ─────────────────────────────────

REQUEST = "Open a pull request for the login fix on the repo and watch its CI"
PUBLISH_NOTE = (
    "[Harness note, not from the user] The publish request above was approved and has gone out; "
    "that request is spent. Carry on with the request this chat is working on."
)


def _chat(note_meta):
    return [
        {"role": "user", "content": REQUEST},
        {"role": "assistant", "content": "I asked to publish the branch."},
        {"role": "user", "content": PUBLISH_NOTE, **({"metadata": note_meta} if note_meta is not None else {})},
    ]


@pytest.mark.parametrize("meta", [{"source": "publish_decision"}, None])
def test_a_harness_note_is_not_the_latest_request(meta):
    """Flagged by metadata, or by its fixed prefix when it carries none (the
    worker-result judgement note has no metadata)."""
    messages = _chat(meta)
    assert al._latest_user_is_harness_note(messages)
    assert al._extract_last_user_message(messages) == REQUEST
    assert al._person_request_text(messages) == REQUEST


def test_every_harness_note_kind_is_recognised():
    from src.agent_control import _ALREADY_ANSWERED_NOTE, _PUBLISH_FOLLOWUP_NOTE

    assert _ALREADY_ANSWERED_NOTE.startswith(al.HARNESS_NOTE_PREFIX)
    assert _PUBLISH_FOLLOWUP_NOTE.startswith(al.HARNESS_NOTE_PREFIX)
    for text, meta in (
        (_ALREADY_ANSWERED_NOTE, None),
        (_PUBLISH_FOLLOWUP_NOTE.format(budget=""), {"source": "publish_decision"}),
        ("[Worker Lead Engineer finished]\nTask: x\n\nResult:\ny", {"source": "worker"}),
        ("1. screenshot", {"source": "tool_images"}),
    ):
        msg = {"role": "user", "content": text, **({"metadata": meta} if meta else {})}
        assert al._is_harness_note(msg), text
    assert not al._is_harness_note({"role": "user", "content": "please publish this"})
    assert not al._is_harness_note({"role": "assistant", "content": PUBLISH_NOTE})


def test_a_chat_of_only_notes_falls_back_to_the_note():
    only = [{"role": "user", "content": PUBLISH_NOTE, "metadata": {"source": "publish_decision"}}]
    assert al._extract_last_user_message(only) == PUBLISH_NOTE


def test_intent_for_a_note_is_a_continuation_of_the_request_not_the_note():
    messages = _chat({"source": "publish_decision"})
    intent = al._classify_agent_request(messages, al._extract_last_user_message(messages))
    assert intent["continuation"] is True and not intent["low_signal"]
    assert intent["retrieval_query"] == REQUEST
    # The note's own words ("approved", "request", "publish", "chat") route nothing.
    note_only = al._classify_agent_request(
        [{"role": "user", "content": "ok"}, {"role": "assistant", "content": "done"},
         {"role": "user", "content": PUBLISH_NOTE, "metadata": {"source": "publish_decision"}}],
        "ok")
    assert note_only["continuation"] is True and not note_only["domains"]


@pytest.fixture(autouse=True)
def _fresh_chat_memory(monkeypatch):
    """Each test's loop runs start with no remembered chat state.

    stream_agent_loop keeps each session's offered tools (_STICKY_TOOLS, which
    only grow) and preface in module-level dicts. These tests reuse session
    ids, so without this the second-loop discovery of manage_calendar in one
    test was already offered in another test's first round.
    """
    monkeypatch.setattr(al, "_STICKY_TOOLS", collections.OrderedDict())
    monkeypatch.setattr(al, "_CHAT_PREFACES", collections.OrderedDict())


def _run_loop(monkeypatch, messages, session_settings=None, relevant=None, url=CHATGPT, model="gpt-6-luna",
              session_id="chat-note"):
    import core.database as db

    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    monkeypatch.setattr(db, "get_session_settings", lambda sid, **_k: dict(session_settings or {}))
    seen = []

    async def _fake_stream(_candidates, msgs, **kwargs):
        factory = kwargs.get("candidate_request_factory")
        request = await factory(0, url, model, {}) if factory else {}
        seen.append({"top": kwargs, "candidate": (request or {}).get("kwargs") or {},
                     "messages": [dict(m) for m in msgs]})
        yield f'data: {json.dumps({"delta": "ok"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    async def _go():
        return [c async for c in al.stream_agent_loop(
            url, model, messages, max_rounds=2,
            relevant_tools=None if relevant is None else set(relevant), session_id=session_id)]

    asyncio.run(_go())
    return seen[0]


@pytest.mark.parametrize("meta", [{"source": "publish_decision"}, None])
def test_the_loop_routes_a_note_on_the_previous_request_with_no_loadout_fit(monkeypatch, meta):
    import src.loadout_routing as lr

    asked = []
    monkeypatch.setattr(lr, "suggest_loadouts", lambda text, *a, **k: asked.append(text) or [
        {"name": "Copywriter", "reason": "the request mentions publish"}])
    intents = []
    real = al._classify_agent_request

    def spy(messages, last_user):
        out = real(messages, last_user)
        intents.append((last_user, out))
        return out

    monkeypatch.setattr(al, "_classify_agent_request", spy)
    seen = _run_loop(monkeypatch, _chat(meta))

    assert intents and intents[0][0] == REQUEST and intents[0][1]["continuation"] is True
    assert asked == [], "a harness note must not be matched against saved loadouts"
    assert not any("Copywriter" in str(m.get("content")) for m in seen["messages"])


def test_a_real_request_still_gets_its_loadout_suggestion(monkeypatch):
    import src.loadout_routing as lr

    asked = []
    monkeypatch.setattr(lr, "suggest_loadouts", lambda text, *a, **k: asked.append(text) or [])
    _run_loop(monkeypatch, [{"role": "user", "content": REQUEST}])
    assert asked == [REQUEST]


# ── 2. a bounded loadout declares its whole set up front ─────────────────────

def _schema(name, desc="d"):
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": {}}}}


def _names(schemas):
    return [stable_tools._name(s) for s in schemas]


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    stable_tools.reset_for_tests()
    yield
    stable_tools.reset_for_tests()


def test_declare_with_full_sends_the_whole_set_then_never_changes(_isolated):
    full = [_schema("read_file"), _schema("grep"), _schema("ls")]
    first, callable_ = stable_tools.declare("w1", [_schema("read_file")], full=full)
    assert _names(first) == ["read_file", "grep", "ls"] and callable_ == ["read_file"]
    # discover_tools makes grep callable: the declared bytes do not move.
    again, callable_ = stable_tools.declare("w1", [_schema("read_file"), _schema("grep")], full=full)
    assert json.dumps(again, sort_keys=True) == json.dumps(first, sort_keys=True)
    assert callable_ == ["read_file", "grep"]


def test_bounded_fits_caps_tools_and_tokens(monkeypatch):
    few = [_schema(f"t{i}") for i in range(5)]
    assert stable_tools.bounded_fits(few)
    assert not stable_tools.bounded_fits([])
    assert not stable_tools.bounded_fits([_schema(f"t{i}") for i in range(stable_tools.BOUNDED_MAX_TOOLS + 1)])
    monkeypatch.setattr(stable_tools, "BOUNDED_MAX_TOKENS", 10)
    assert not stable_tools.bounded_fits(few)


BOUNDED_POLICY = {"tool_access": "selected", "enabled_tools": ["read_file", "grep", "ls", "manage_calendar"]}


def test_loop_declares_a_loadouts_whole_allowed_set_in_round_one(monkeypatch, _isolated):
    first = _run_loop(monkeypatch, [{"role": "user", "content": "check the repo"}], BOUNDED_POLICY,
                      relevant={"read_file"}, session_id="worker-bounded")
    declared = set(_names(first["top"]["tools"]))
    assert {"read_file", "grep", "ls", "manage_calendar"} <= declared
    # Only what the allow-list grants (plus the always-available helpers) is declared.
    assert "bash" not in declared and "manage_git" not in declared
    # The round's selection stays callable-only: the calendar is declared, not offered.
    assert "manage_calendar" not in first["top"]["allowed_tools"]
    assert "read_file" in first["top"]["allowed_tools"]


def test_a_later_discovery_of_an_allowed_tool_leaves_the_tools_hash_unchanged(monkeypatch, _isolated):
    first = _run_loop(monkeypatch, [{"role": "user", "content": "check the repo"}], BOUNDED_POLICY,
                      relevant={"read_file"}, session_id="worker-bounded")
    stable_tools._DECLARED.clear()  # as after a restart: the persisted list is re-read
    second = _run_loop(monkeypatch, [{"role": "user", "content": "check the repo"}], BOUNDED_POLICY,
                       relevant={"read_file", "manage_calendar"}, session_id="worker-bounded")
    assert llm_core._short_hash(_payload_tools(first)) == llm_core._short_hash(_payload_tools(second))
    assert "manage_calendar" in second["top"]["allowed_tools"]


def _payload_tools(seen):
    payload = _build_chatgpt_responses_payload(
        "gpt-6-luna", MSGS, 0.2, 0, tools=seen["top"]["tools"], target_url_hint=CHATGPT,
        cache_key="worker-bounded", allowed_tools=seen["top"]["allowed_tools"])
    return payload["tools"]


def test_a_chat_with_every_tool_allowed_keeps_growing_as_needed(monkeypatch, _isolated):
    first = _run_loop(monkeypatch, [{"role": "user", "content": "check the repo"}], {},
                      relevant={"read_file"}, session_id="admin-chat")
    assert "manage_calendar" not in _names(first["top"]["tools"])
    second = _run_loop(monkeypatch, [{"role": "user", "content": "check the repo"}], {},
                       relevant={"read_file", "manage_calendar"}, session_id="admin-chat")
    assert _names(second["top"]["tools"])[-1] == "manage_calendar"


def test_an_allow_list_past_the_cap_is_not_declared_whole(monkeypatch, _isolated):
    monkeypatch.setattr(stable_tools, "BOUNDED_MAX_TOOLS", 2)
    first = _run_loop(monkeypatch, [{"role": "user", "content": "check the repo"}], BOUNDED_POLICY,
                      relevant={"read_file"}, session_id="worker-big")
    assert "manage_calendar" not in _names(first["top"]["tools"])


# ── 3. prompt_cache_retention ────────────────────────────────────────────────

@pytest.fixture
def _retention(monkeypatch):
    for _s in (llm_core._RESPONSES_NO_CACHE_RETENTION, llm_core._RESPONSES_CACHE_RETENTION_OK,
               llm_core._RESPONSES_CACHE_RETENTION_SUSPECT):
        _s.clear()
    llm_core._REJECTED_REQUEST_PARAMS.clear()
    values = {"chatgpt_prompt_cache_retention": "24h"}
    import src.settings as settings

    real = settings.get_setting
    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: values[key] if key in values else real(key, default))
    yield values
    for _s in (llm_core._RESPONSES_NO_CACHE_RETENTION, llm_core._RESPONSES_CACHE_RETENTION_OK,
               llm_core._RESPONSES_CACHE_RETENTION_SUSPECT):
        _s.clear()
    llm_core._RESPONSES_NO_CACHE_RETENTION.update(llm_core._KNOWN_NO_CACHE_RETENTION)
    llm_core._REJECTED_REQUEST_PARAMS.clear()


def _build(**kw):
    return _build_chatgpt_responses_payload("gpt-6-sol", MSGS, 1.0, 0, target_url_hint=CHATGPT, **kw)


def test_retention_is_sent_by_default(_retention):
    assert _build()["prompt_cache_retention"] == "24h"


def test_the_setting_can_turn_retention_off_or_change_it(_retention):
    _retention["chatgpt_prompt_cache_retention"] = ""
    assert "prompt_cache_retention" not in _build()
    _retention["chatgpt_prompt_cache_retention"] = "in_memory"
    assert _build()["prompt_cache_retention"] == "in_memory"


def test_the_default_setting_is_24h():
    from src.settings import DEFAULT_SETTINGS as DEFAULTS

    assert DEFAULTS["chatgpt_prompt_cache_retention"] == "24h"


@pytest.mark.parametrize("body,expected", [
    ('{"error":{"message":"Unsupported parameter: prompt_cache_retention"}}', True),
    ('{"detail":"Unknown parameter: \'prompt_cache_retention\'."}', True),
    ('{"error":{"message":"Rate limit reached for gpt-6-sol"}}', False),
    ('{"error":{"message":"The model `gpt-9` does not exist"}}', False),
    ('{"error":{"message":"Unsupported parameter: include"}}', False),
    ('{"error":{"message":"prompt_cache_key too long"}}', False),
])
def test_only_a_rejection_naming_the_field_matches(body, expected):
    assert llm_core._mentions_cache_retention(body) is expected


def _fake_backend(monkeypatch, reject):
    """A client answering 400 with ``reject`` while the payload carries the field."""
    sent = []

    class _Resp:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 400 if reject(payload) else 200

        async def aread(self):
            return (reject(self.payload) or "").encode()

        async def aiter_lines(self):
            for event in (
                {"type": "response.output_text.delta", "delta": "hello"},
                {"type": "response.completed", "response": {}},
            ):
                yield f"data: {json.dumps(event)}"

    class _Stream:
        def __init__(self, payload):
            self.payload = payload

        async def __aenter__(self):
            return _Resp(self.payload)

        async def __aexit__(self, *a):
            return False

    class _Client:
        def stream(self, method, url, **kwargs):
            sent.append(dict(kwargs["json"]))
            return _Stream(kwargs["json"])

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _Client())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda url: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    return sent


async def _stream_once():
    return [c async for c in llm_core.stream_llm(
        "https://chatgpt.com/backend-api/codex", "gpt-5.6-sol", [{"role": "user", "content": "hi"}])]


def test_a_400_naming_the_field_disables_it_for_the_host_and_the_retry_succeeds(monkeypatch, _retention):
    sent = _fake_backend(
        monkeypatch,
        lambda p: "Unsupported parameter: prompt_cache_retention" if "prompt_cache_retention" in p else "")
    chunks = asyncio.run(_stream_once())
    text = "".join(chunks)
    assert "hello" in text, "the retry without the field must reach the caller"
    assert "event: error" not in text and "prompt_cache_retention" not in text, "the user never sees the 400"
    assert "prompt_cache_retention" in sent[0] and "prompt_cache_retention" not in sent[-1]
    assert llm_core._host_key("https://chatgpt.com/backend-api/codex") in llm_core._RESPONSES_NO_CACHE_RETENTION
    # Later requests to that host no longer carry it.
    assert "prompt_cache_retention" not in _build()


def test_an_unrelated_400_does_not_disable_retention(monkeypatch, _retention):
    _fake_backend(monkeypatch, lambda p: "Rate limit reached for requests")
    chunks = asyncio.run(_stream_once())
    assert "event: error" in "".join(chunks)
    assert not llm_core._RESPONSES_NO_CACHE_RETENTION
    assert _build()["prompt_cache_retention"] == "24h"


def test_a_generic_400_on_an_unconfirmed_host_retries_without_the_field(monkeypatch, _retention):
    """The backend may reject an unknown field without naming it."""
    sent = _fake_backend(monkeypatch, lambda p: "Bad Request" if "prompt_cache_retention" in p else "")
    text = "".join(asyncio.run(_stream_once()))
    assert "hello" in text and "event: error" not in text
    assert "prompt_cache_retention" in sent[0] and "prompt_cache_retention" not in sent[-1]
    assert "prompt_cache_retention" not in _build()


def test_a_generic_400_on_a_confirmed_host_keeps_the_field(monkeypatch, _retention):
    llm_core._RESPONSES_CACHE_RETENTION_OK.add(llm_core._host_key("https://chatgpt.com/backend-api/codex"))
    _fake_backend(monkeypatch, lambda p: "Bad Request")
    assert "event: error" in "".join(asyncio.run(_stream_once()))
    assert _build()["prompt_cache_retention"] == "24h"


def test_the_chatgpt_backend_is_known_not_to_take_the_field():
    """It answered 400 on both process starts in the 2026-10-02 logs."""
    assert "https://chatgpt.com" in llm_core._RESPONSES_NO_CACHE_RETENTION
    assert "prompt_cache_retention" not in _build()

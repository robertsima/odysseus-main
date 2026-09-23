"""Tests for the session tools' move to the agent_tools registry (#3629):
create_session, list_sessions, send_to_session, manage_session.

The implementations now live in src/agent_tools/session_tools.py (moved out of
src/ai_interaction.py). These assert (1) the handlers are registered in
TOOL_HANDLERS, (2) the moved logic runs and threads owner/session from ctx
(the session manager is fetched via ai_interaction.get_session_manager), and
(3) tool_execution.py dispatches them through the registry rather than the
legacy dispatch_ai_tool elif.
"""
import asyncio
from pathlib import Path

import src.ai_interaction as ai_interaction
import src.database as database
from src.agent_tools import TOOL_HANDLERS
from src.agent_tools import session_tools as st

_SESSION_TOOLS = ("create_session", "list_sessions", "send_to_session", "manage_session")


def test_session_tools_registered():
    for name in _SESSION_TOOLS:
        assert name in TOOL_HANDLERS, f"{name} missing from TOOL_HANDLERS"


def test_list_sessions_handler_threads_ctx(monkeypatch):
    # The handler must thread content + session_id + owner from ctx into the
    # moved list_sessions implementation. Spy at the function boundary so the
    # test does not depend on list_sessions' DB internals.
    seen = {}

    async def spy(content, session_id=None, owner=None):
        seen.update(content=content, session_id=session_id, owner=owner)
        return {"results": "ok"}

    monkeypatch.setattr(st, "list_sessions", spy)
    res = asyncio.run(st.ListSessionsTool().execute("q", {"owner": "alice", "session_id": "s1"}))
    assert res == {"results": "ok"}
    assert seen == {"content": "q", "session_id": "s1", "owner": "alice"}


def test_manage_session_list_delegates_to_list_sessions(monkeypatch):
    # manage_session("list") must delegate to list_sessions; guards against a
    # stale do_list_sessions reference surviving the move (caught live in e2e).
    called = {}

    async def spy(content, session_id=None, owner=None):
        called["owner"] = owner
        return {"results": "ok"}

    monkeypatch.setattr(st, "list_sessions", spy)
    # manage_session imports `Session` from src.database before the list branch;
    # the src.database test double may not expose it, so provide a stand-in.
    monkeypatch.setattr(database, "Session", object, raising=False)
    monkeypatch.setattr(ai_interaction, "_session_manager", object())  # truthy: pass the guard
    res = asyncio.run(st.ManageSessionTool().execute("list", {"owner": "carol"}))
    assert called.get("owner") == "carol"
    assert res == {"results": "ok"}


def test_create_session_reaches_uuid_and_creates(monkeypatch):
    # Regression for the missing `import uuid` (PR review): create_session must
    # get past _resolve_model and mint a session id without NameError.
    monkeypatch.setattr(st, "_resolve_model", lambda spec, owner=None: ("http://x", "model-x", {}))
    created = {}

    class FakeMgr:
        def create_session(self, **kw):
            created.update(kw)

        def get_session(self, sid):
            return None

    monkeypatch.setattr(ai_interaction, "_session_manager", FakeMgr())
    res = asyncio.run(st.CreateSessionTool().execute("My Chat\nmodel-x", {"owner": "alice"}))
    assert res.get("name") == "My Chat" and res.get("model") == "model-x"
    assert isinstance(res.get("session_id"), str) and res["session_id"]
    assert created.get("name") == "My Chat"  # the uuid-minted id reached the manager


def test_manage_session_fork_reaches_uuid(monkeypatch):
    # Regression for the missing `import uuid`: the fork action also mints a new
    # session id and must not NameError. Mocks the DB query layer so the fork
    # branch reaches the uuid call without a real sessions table.
    class FakeDbSession:
        id = "id"
        owner = "owner"

    class FakeQ:
        def filter(self, *a, **k):
            return self

        def first(self):
            return object()

    class FakeDB:
        def query(self, *a, **k):
            return FakeQ()

        def close(self):
            pass

    monkeypatch.setattr(database, "Session", FakeDbSession, raising=False)
    monkeypatch.setattr(database, "SessionLocal", lambda: FakeDB(), raising=False)

    class Src:
        name = "Orig"
        endpoint_url = "http://x"
        model = "m"

        def get_context_messages(self):
            return []

    created = {}

    class FakeMgr:
        def get_session(self, sid):
            return Src() if sid == "abc" else type("S", (), {"add_message": lambda self, m: None})()

        def create_session(self, **kw):
            created.update(kw)

    monkeypatch.setattr(ai_interaction, "_session_manager", FakeMgr())
    res = asyncio.run(st.ManageSessionTool().execute('{"action":"fork","session_id":"abc"}', {"owner": "owner"}))
    assert res.get("action") == "fork"
    assert isinstance(res.get("session_id"), str) and res["session_id"]
    assert created.get("name") == "Fork: Orig"  # uuid-minted new session was created


def test_no_session_manager_is_handled(monkeypatch):
    # With no session manager set, the moved function must fail gracefully
    # (proves the handler reached the impl, not an "unknown tool").
    monkeypatch.setattr(ai_interaction, "_session_manager", None)
    res = asyncio.run(st.ListSessionsTool().execute("", {"owner": "bob"}))
    assert isinstance(res, dict)
    assert "error" in res or "results" in res


class _FakeSession:
    def __init__(self, owner, name, history):
        self.owner = owner
        self.name = name
        self.endpoint_url = "http://x"
        self.model = "fixture-tool-model"  # offline path: returns transcript, no network
        self._history = history
        self.added = []

    def get_context_messages(self):
        return list(self._history)

    def add_message(self, m):
        self.added.append(m)


class _FakeMgr:
    def __init__(self, sessions):
        self._s = sessions

    def get_session(self, sid):
        return self._s.get(sid)


def test_send_to_session_blocks_null_owner_for_authenticated_caller(monkeypatch):
    # An authenticated caller must not reach a null-owner (legacy / auth-was-off)
    # session: list_sessions and manage_session already hide those, so this path
    # was the inconsistency — it let an agent read/write a session the other
    # tools exclude. Mirrors the calendar owner=None hardening.
    null_sess = _FakeSession(None, "Secret", [{"role": "user", "content": "PIN 4321"}])
    bob_sess = _FakeSession("bob", "Bob", [{"role": "user", "content": "bob secret"}])
    monkeypatch.setattr(st, "get_session_manager",
                        lambda: _FakeMgr({"nsid": null_sess, "bsid": bob_sess}))

    # authenticated alice: null-owner session is not-found and its history is not leaked
    r = asyncio.run(st.send_to_session("nsid\nhello", owner="alice"))
    assert r.get("error", "").endswith("not found")
    assert "4321" not in str(r)
    assert null_sess.added == []  # nothing written into it either

    # authenticated alice still cannot reach another real user's session
    r2 = asyncio.run(st.send_to_session("bsid\nhello", owner="alice"))
    assert r2.get("error", "").endswith("not found")

    # auth disabled (no owner): single-user still reaches the null-owner session
    r3 = asyncio.run(st.send_to_session("nsid\nhello", owner=None))
    assert r3.get("offline_transcript") is True


def test_dispatched_via_registry_not_dispatch_ai_tool():
    source = (Path(__file__).resolve().parent.parent / "src" / "tool_execution.py").read_text(encoding="utf-8")
    assert 'elif tool in ("create_session", "list_sessions", "send_to_session", "manage_session"):' in source

    marker = "from src.ai_interaction import dispatch_ai_tool"
    idx = source.index(marker)
    branch_head = source.rfind("elif tool in (", 0, idx)
    legacy_tuple = source[branch_head:idx]
    for name in _SESSION_TOOLS:
        assert f'"{name}"' not in legacy_tuple, f"{name} still routed via dispatch_ai_tool"


# ── a failed hand-off must not read as a success ──────────────────────────
#
# The agent loop and the activity feed both decide a tool failed with
# `result.get("exit_code") not in (0, None)`, so an error return with no
# exit_code at all reads as "done": a `send_to_session` that never delivered
# rendered as a finished tool bubble and a green activity row.


def _failed(result):
    """The exact predicate src/agent_loop.py applies to a tool result."""
    return result.get("exit_code") not in (0, None)


def test_send_to_session_errors_report_as_failures(monkeypatch):
    monkeypatch.setattr(st, "get_session_manager", lambda: _FakeMgr({}))

    # An unknown target, a missing message and a bad mode are all real failures
    # the agent has to see as failures.
    for content in ('{"session_id": "nope", "message": "hi"}',
                    '{"session_id": "nope", "message": ""}',
                    '{"session_id": "nope", "message": "hi", "mode": "sideways"}'):
        res = asyncio.run(st.send_to_session(content, owner="alice"))
        assert "error" in res, content
        assert _failed(res), f"{content} -> reported as success: {res}"


def test_manage_session_errors_report_as_failures(monkeypatch):
    monkeypatch.setattr(st, "get_session_manager", lambda: _FakeMgr({}))
    monkeypatch.setattr(database, "Session", object, raising=False)

    for content in ("", '{"action": "teleport", "session_id": "abc"}'):
        res = asyncio.run(st.manage_session(content, owner="alice"))
        assert "error" in res, content
        assert _failed(res), f"{content} -> reported as success: {res}"


# ── a loadout means one thing, because the other one is refused ───────────
#
# `session_id: "new"` writes the loadout's whole policy onto the chat it
# creates. An existing chat is the user's: patching a loadout's policy onto it
# would silently re-police somebody else's chat off the back of one delegated
# message, and applying only the parts that fit would mean the same argument
# meant two different policies. `send_to_session` refuses that combination
# outright instead, which is the stronger form of the same argument — and the
# result still says which policy it applied, because the scheduler and the
# crew-role link do apply a loadout to a chat they did not create.


_LOADOUT = {
    "name": "Auditor", "instructions": "", "model": "", "model_fallbacks": [],
    "disabled_tools": ["bash"], "max_rounds": 5, "private_vault_access": False,
}


def _run_send(monkeypatch, target, seen):
    """Send to `target` under _LOADOUT, with the headless drain stubbed out."""
    import src.agent_profiles as agent_profiles
    import src.headless_agent as headless

    existing = _FakeSession("alice", "Notes", [])
    existing.model = "m"  # not the offline fixture model: take the agent path

    async def fake_headless(sess, messages, **kwargs):
        seen.update(kwargs)
        return "done", []

    monkeypatch.setattr(headless, "run_headless", fake_headless)
    monkeypatch.setattr(agent_profiles, "get_profile", lambda name: dict(_LOADOUT))
    monkeypatch.setattr(st, "get_session_manager", lambda: _FakeMgr({"sid": existing}))
    monkeypatch.setattr(st, "_new_child_session", lambda *a, **k: (_Child(), None))
    return asyncio.run(st.send_to_session(
        '{"session_id": "%s", "message": "audit", "profile": "Auditor"}' % target, owner="alice"))


class _Child(_FakeSession):
    def __init__(self):
        super().__init__("alice", "child", [])
        self.id, self.model = "childsid", "m"


def test_a_loadout_says_it_re_policed_the_chat_it_created(monkeypatch):
    fresh = _run_send(monkeypatch, "new", {})
    assert fresh["profile"] == "Auditor"
    assert fresh["profile_scope"] == "chat"
    # The scope is spelled out rather than left to be discovered.
    assert "full policy applies" in fresh["profile_note"]


def test_a_loadout_aimed_at_an_existing_chat_is_refused_not_half_applied(monkeypatch):
    """The half-applied case is gone: it is refused before anything runs.

    A partially applied loadout was reported honestly, but it still meant one
    argument could mean two policies. Refusing it is the narrower answer and
    the error names the fix.
    """
    existing = _run_send(monkeypatch, "sid", {})
    assert _failed(existing)
    assert "new" in existing["error"]
    assert "profile" not in existing


def test_a_loadouts_private_vault_denial_binds_an_existing_chat_too(monkeypatch):
    """Defence in depth on the run itself, not only on the stored settings.

    `session_patch` writes `private_vault_access: False` onto the child chat,
    but `run_headless` reads the grant from that chat mid-run, so a loadout
    that denies the vault also says so on the call. Narrowing is always safe:
    the flag can take access away and can never hand it to a chat with none.
    """
    seen = {}
    _run_send(monkeypatch, "new", seen)
    assert seen["deny_private_vault"] is True

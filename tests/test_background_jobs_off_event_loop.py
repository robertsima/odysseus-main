"""Hourly background jobs must do their blocking work off the event loop.

Production, 2026-09-28, app.log [loop-lag] warnings, each one freezing every
open tab for the duration:

* 00:15 / 01:00 — 1.5-1.8 s in ``ssl.read`` <- email_pollers
  ``_auto_summarize_pass_single`` (an RFC822 FETCH) <- action_extract_email_events
* 02:00 — 2.72 s in ``islink`` <- skills ``_iter_skill_files`` <- ``load`` <-
  skills_routes ``_audit_one_skill`` <- ``_run_audit_all_job``
* 00:27 — 1.15 s in SQL <- ``resolve_endpoint`` <- ``_try_ai_tidy_group`` <-
  ``action_consolidate_memory``

Each test records the thread every blocking call ran on and requires it to be
a worker thread, not the loop's (pattern: tests/test_event_loop_starvation.py).
"""
from __future__ import annotations

import asyncio
import threading
from email.message import EmailMessage

import pytest


class _ThreadLog:
    def __init__(self):
        self.loop_thread = threading.get_ident()
        self.calls = []

    def hit(self, name):
        self.calls.append((name, threading.get_ident()))

    def on_loop(self):
        return sorted({name for name, tid in self.calls if tid == self.loop_thread})

    def names(self):
        return {name for name, _ in self.calls}


# ── email auto-summarize pass ───────────────────────────────────────────── #


def _raw_email():
    msg = EmailMessage()
    msg["From"] = "Sam <sam@example.com>"
    msg["To"] = "me@example.com"
    msg["Subject"] = "Quarterly numbers"
    msg["Message-ID"] = "<q-1@example.com>"
    msg["Date"] = "Sun, 27 Sep 2026 23:00:00 +0000"
    msg.set_content("Here are the quarterly numbers we discussed. " * 10)
    return msg.as_bytes()


async def test_email_summarize_pass_does_imap_and_sqlite_off_the_loop(monkeypatch):
    import routes.email_pollers as ep

    log = _ThreadLog()

    class _Conn:
        def select(self, folder, readonly=True):
            log.hit("imap.select")
            return "OK", [b"1"]

        def uid(self, cmd, *args):
            log.hit(f"imap.{cmd.lower()}")
            if cmd == "SEARCH":
                return "OK", [b"7"]
            if cmd == "FETCH":
                return "OK", [(b"7 (RFC822 {1}", _raw_email())]
            return "OK", [None]

        def logout(self):
            log.hit("imap.logout")

    def fake_connect(account_id=None, owner=""):
        log.hit("imap.connect")
        return _Conn()

    def fake_owner(account_id):
        log.hit("owner_lookup")
        return "alice"

    def fake_cached(*args, **kwargs):
        log.hit("sqlite.read_cache")
        return set(), set(), set(), set(), set()

    def fake_write(sql, params):
        log.hit("sqlite.write")

    def fake_candidates(owner=None, **kwargs):
        log.hit("resolve_task_candidates")
        return [("http://model/v1", "m", {})]

    def fake_config(account_id=None, owner=""):
        log.hit("email_config")
        return {"from_address": "me@example.com"}

    async def fake_summary(**kwargs):
        return "A short summary."

    monkeypatch.setattr(ep, "_imap_connect", fake_connect)
    monkeypatch.setattr(ep, "_owner_for_email_account", fake_owner)
    monkeypatch.setattr(ep, "_load_settings", lambda: {"email_auto_summarize": True})
    monkeypatch.setattr(ep, "_load_cached_message_ids", fake_cached)
    monkeypatch.setattr(ep, "_cache_write", fake_write)
    monkeypatch.setattr(ep, "resolve_task_candidates", fake_candidates)
    monkeypatch.setattr(ep, "_get_email_config", fake_config)
    monkeypatch.setattr(ep, "_generate_scheduled_email_summary", fake_summary)

    result = await ep._auto_summarize_pass_single(account_id="acct-1")

    assert "summarized 1" in result, result
    assert {"imap.connect", "imap.search", "imap.fetch", "imap.logout",
            "sqlite.read_cache", "sqlite.write", "resolve_task_candidates"} <= log.names()
    assert log.on_loop() == [], f"blocking calls ran on the event loop: {log.on_loop()}"


def test_fetch_helper_parses_the_message():
    import routes.email_pollers as ep

    class _Conn:
        def uid(self, cmd, uid, spec):
            assert (cmd, uid, spec) == ("FETCH", b"7", "(RFC822)")
            return "OK", [(b"7 (RFC822 {1}", _raw_email())]

    st, msg = ep._fetch_rfc822(_Conn(), b"7")
    assert st == "OK"
    assert msg["Message-ID"] == "<q-1@example.com>"


# ── skills audit ────────────────────────────────────────────────────────── #


class _Skills:
    """A SkillsManager stand-in that records where each call ran."""

    def __init__(self, log):
        self.log = log
        self.rows = [{"name": "alpha", "owner": "alice", "description": "d", "status": "draft"},
                     {"name": "beta", "owner": "alice", "description": "d2", "status": "draft"}]

    def load(self, owner=None):
        self.log.hit("load")
        return [dict(r) for r in self.rows]

    def read_skill_md(self, name, owner=None):
        self.log.hit("read_skill_md")
        return "---\nname: alpha\n---\nbody"

    def set_necessity(self, *a, **k):
        self.log.hit("set_necessity")

    def set_audit(self, *a, **k):
        self.log.hit("set_audit")

    def update_skill(self, *a, **k):
        self.log.hit("update_skill")


async def test_skill_audit_touches_the_skills_store_off_the_loop(monkeypatch):
    import routes.skills_routes as sr

    log = _ThreadLog()

    async def fake_necessity(*a, **k):
        return {"necessary": True}

    async def fake_test_once(*a, **k):
        return "transcript", {"verdict": "pass", "issues": [], "summary": "ok"}

    def fake_duplicate(sm, name, owner):
        log.hit("duplicate_blocker")
        return None

    def fake_finalize(sm, name, owner, *a, **k):
        log.hit("finalize_status")
        return "published"

    monkeypatch.setattr(sr, "_eval_skill_necessity", fake_necessity)
    monkeypatch.setattr(sr, "_run_skill_test_once", fake_test_once)
    monkeypatch.setattr(sr, "_skill_duplicate_blocker", fake_duplicate)
    monkeypatch.setattr(sr, "_audit_finalize_status", fake_finalize)
    monkeypatch.setattr(sr, "_audit_generic_blocker", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_should_check_retrieval_precision", lambda skill: False)

    key = ("alice-offloop-test",)
    sr._skill_audit_jobs[key] = {"status": "running", "results": [], "log": [], "done": 0,
                                 "current": None, "cancel": False}
    try:
        await sr._run_audit_all_job(key, _Skills(log), ["alpha"], "http://m/v1", "m", {}, None, "alice")
        job = sr._skill_audit_jobs[key]
    finally:
        sr._skill_audit_jobs.pop(key, None)

    assert job["results"][0]["result"] == "pass", job
    assert {"load", "read_skill_md", "set_necessity", "set_audit", "update_skill",
            "duplicate_blocker", "finalize_status"} <= log.names()
    assert log.on_loop() == [], f"skills-store calls ran on the event loop: {log.on_loop()}"


# ── memory consolidation ────────────────────────────────────────────────── #


async def test_memory_consolidation_blocks_nothing_on_the_loop(monkeypatch):
    import src.memory as memory_mod
    import src.task_endpoint as task_endpoint
    from src import builtin_actions

    log = _ThreadLog()
    saved = {}

    class _Memories:
        def __init__(self, data_dir):
            pass

        def load_all(self):
            log.hit("load_all")
            return [
                {"id": "1", "owner": "alice", "category": "fact", "text": "Alice's dentist is Dr. Rowe on Main Street."},
                {"id": "2", "owner": "alice", "category": "fact", "text": "Alice's dentist is Dr. Rowe on Main Street."},
            ]

        def save(self, memories):
            log.hit("save")
            saved["ids"] = [m["id"] for m in memories]

    def fake_candidates(*a, **k):
        log.hit("resolve_task_candidates")
        return []  # no model: consolidation falls back to plain dedupe

    monkeypatch.setattr(memory_mod, "MemoryManager", _Memories)
    monkeypatch.setattr(task_endpoint, "resolve_task_candidates", fake_candidates)

    result, ok = await builtin_actions.action_consolidate_memory("alice")

    assert ok, result
    assert "Removed 1 duplicate" in result
    assert saved["ids"] == ["1"] or saved["ids"] == ["2"]
    assert {"load_all", "save"} <= log.names()
    assert log.on_loop() == [], f"blocking calls ran on the event loop: {log.on_loop()}"


async def test_memory_endpoint_lookup_runs_off_the_loop_when_a_group_can_use_ai(monkeypatch):
    import src.memory as memory_mod
    import src.task_endpoint as task_endpoint
    from src import builtin_actions

    log = _ThreadLog()

    class _Memories:
        def __init__(self, data_dir):
            pass

        def load_all(self):
            return [
                {"id": "1", "owner": "alice", "category": "fact", "text": "Alice likes green tea in the morning."},
                {"id": "2", "owner": "alice", "category": "project", "text": "Odysseus runs on the ZimaOS NAS."},
            ]

        def save(self, memories):
            pass

    def fake_candidates(*a, **k):
        log.hit("resolve_task_candidates")
        return []

    monkeypatch.setattr(memory_mod, "MemoryManager", _Memories)
    monkeypatch.setattr(task_endpoint, "resolve_task_candidates", fake_candidates)

    from src.builtin_actions import TaskNoop
    # Nothing to remove: the action reports a no-op (TaskNoop is a
    # BaseException, so it passes the action's broad except).
    with pytest.raises(TaskNoop):
        await builtin_actions.action_consolidate_memory("alice")

    assert "resolve_task_candidates" in log.names()
    assert log.on_loop() == []

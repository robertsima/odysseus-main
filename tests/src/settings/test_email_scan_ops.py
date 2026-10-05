"""A requested email scan carries its operations instead of rewriting the
global ``email_auto_*`` settings (2026-09-28).

``_run_auto_summarize_once`` used to write the scheduled task's flags into
settings.json for the whole scan and restore them afterwards: the background
poller acted on those flags meanwhile, a UI save inside the window was undone
by the restore, and the ``_email_auto_reply_draft_only`` marker was never
removed (the restore wrote ``False`` back). These tests pin that the scan no
longer touches settings, that the pass does exactly the requested operations,
and that the leftover marker is removed.
"""
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import routes.email_pollers as ep


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """One real settings.json behind both the email helpers and src.settings."""
    import routes.email_helpers as helpers
    import src.settings as settings_mod

    path = tmp_path / "settings.json"
    monkeypatch.setattr(helpers, "SETTINGS_FILE", path)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(path))
    settings_mod._invalidate_caches()
    monkeypatch.setattr(ep, "_leftover_scan_key_checked", False)
    return path


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


async def test_requested_scan_passes_its_ops_and_leaves_settings_alone(settings_file, monkeypatch):
    saved = {"email_auto_summarize": False, "email_auto_reply": True, "email_auto_tag": True}
    _write(settings_file, saved)
    seen = {}

    async def fake_pass(**kwargs):
        # The poller reads settings.json while a scan runs; it must see the
        # user's flags, not the scheduled task's.
        seen["during"] = json.loads(settings_file.read_text(encoding="utf-8"))
        seen["kwargs"] = kwargs
        return "Processed 0 emails"

    monkeypatch.setattr(ep, "_auto_summarize_pass", fake_pass)
    monkeypatch.setattr(ep, "_save_settings", lambda s: pytest.fail("the scan wrote settings.json"))

    result = await ep._run_auto_summarize_once(
        do_summary=True, do_reply=False, do_tag=False, do_spam=True, do_calendar=True,
        days_back=3, account_id="acct-1", max_process=4,
    )

    assert result == "Processed 0 emails"
    assert seen["during"] == saved
    assert json.loads(settings_file.read_text(encoding="utf-8")) == saved
    kwargs = seen["kwargs"]
    assert kwargs["ops"] == ep.ScanOps(summary=True, reply_draft=False, tag=False, spam=True, calendar=True)
    assert (kwargs["days_back"], kwargs["account_id"], kwargs["max_process"]) == (3, "acct-1", 4)


async def test_leftover_draft_only_marker_is_removed(settings_file, monkeypatch):
    _write(settings_file, {"email_auto_reply": True, "_email_auto_reply_draft_only": False, "theme": "dark"})

    async def fake_pass(**kwargs):
        return "ok"

    monkeypatch.setattr(ep, "_auto_summarize_pass", fake_pass)
    await ep._run_auto_summarize_once(do_summary=True, do_reply=False)

    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"email_auto_reply": True, "theme": "dark"}


async def test_no_marker_means_no_write(settings_file, monkeypatch):
    _write(settings_file, {"email_auto_reply": True})
    import src.settings as settings_mod

    monkeypatch.setattr(settings_mod, "save_settings", lambda s: pytest.fail("rewrote settings without a marker"))

    async def fake_pass(**kwargs):
        return "ok"

    monkeypatch.setattr(ep, "_auto_summarize_pass", fake_pass)
    await ep._run_auto_summarize_once()


def test_poller_flags_follow_settings_and_ignore_the_old_marker(monkeypatch):
    monkeypatch.setattr(ep, "_away_reply_active", lambda settings, account_id: True)
    settings = {"email_auto_summarize": True, "email_auto_reply": True,
                "_email_auto_reply_draft_only": True, "email_auto_calendar": True}

    flags = ep._scan_flags(settings, "acct-1", None)

    # (summary, reply_draft, reply_away, tag, spam, calendar): a marker left
    # at true by a dead scan no longer turns the away replies into drafts.
    assert flags == (True, False, True, False, False, True)


def test_requested_flags_are_exactly_the_ops(monkeypatch):
    monkeypatch.setattr(ep, "_away_reply_active", lambda settings, account_id: True)
    # A per-account auto-reply overlay used to leak into requested scans:
    # email_auto_reply=false on the account suppressed the drafts asked for.
    settings = ep._effective_settings_for_email_account(
        {"email_auto_reply": True, "email_auto_summarize": True,
         "email_auto_reply_by_account": {"acct-1": {"email_auto_reply": False}}},
        "acct-1",
    )

    flags = ep._scan_flags(settings, "acct-1", ep.ScanOps(reply_draft=True, tag=True))

    assert flags == (False, True, False, True, False, False)


async def test_pass_performs_requested_ops_with_every_setting_off(monkeypatch):
    """With settings all off the plain pass has nothing to do; the same pass
    asked for a summary produces one."""
    raw = (
        b"From: Bob <bob@example.com>\r\nTo: me@example.com\r\nSubject: Quarterly plan\r\n"
        b"Message-ID: <ops-1@example.com>\r\nDate: Tue, 01 Jan 2026 12:00:00 +0000\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + b"Please review the quarterly plan. " * 8
    )

    class _Conn:
        def select(self, folder, readonly=True):
            return "OK", [b"1"]

        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                return "OK", [b"7"]
            if cmd == "FETCH":
                return "OK", [(b"7 (RFC822 {1}", raw)]
            return "OK", [None]

        def logout(self):
            pass

    async def fake_summary(**kwargs):
        return "A short summary."

    monkeypatch.setattr(ep, "_load_settings", lambda: {"email_auto_summarize": False, "email_auto_reply": False})
    monkeypatch.setattr(ep, "_imap_connect", lambda account_id=None, owner="": _Conn())
    monkeypatch.setattr(ep, "_owner_for_email_account", lambda account_id: "alice")
    monkeypatch.setattr(ep, "_load_cached_message_ids", lambda *a, **k: (set(), set(), set(), set(), set()))
    monkeypatch.setattr(ep, "_cache_write", lambda sql, params: None)
    monkeypatch.setattr(ep, "resolve_task_candidates", lambda owner=None, **k: [("http://model/v1", "m", {})])
    monkeypatch.setattr(ep, "_get_email_config", lambda account_id=None, owner="": {"from_address": "me@example.com"})
    monkeypatch.setattr(ep, "_generate_scheduled_email_summary", fake_summary)

    assert await ep._auto_summarize_pass_single(account_id="acct-1") == "Nothing to do"
    result = await ep._auto_summarize_pass_single(account_id="acct-1", ops=ep.ScanOps(summary=True))
    assert "summarized 1" in result, result

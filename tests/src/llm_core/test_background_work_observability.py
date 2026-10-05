"""Regressions from the 2026-09-15 production logs.

Four defects, all of the same family: the harness spending real money and real
wall-clock on work it then threw away, or on noise that hid the cause.

1. **Watching background work cancelled it.** Every tracked request fires the
   foreground sweep that kills running background tasks. The agents dashboard
   polls `/api/agents/overview` + `/api/agents/approvals` on a 20s timer (5s
   while open) *and* refreshes on the SSE `run_started` event — so a scheduled
   task starting caused the poll that killed it ~1.4s later, forever:

       13:45:02,637  round_start   (task fires a 17.5k-token prompt)
       13:45:04,005  Task 'Applied Job Status Tracker' interrupted …
       13:45:04,021  Stopped 2 background scheduler task(s):
                     foreground request GET /api/agents/approvals

   The same task re-queued at 14:00 and died the same way. It never completed
   once.

2. **Every completed Claude Code delegation shipped ~50KB of duplicate JSONL.**
   In stream mode the raw stdout *is* the transcript's source; both went into
   the result, hence three identical 58095-char offloads in one session, each
   also embedded into Chroma.

3. **A rejected credential walked the whole fallback chain.** 401 means
   reconnect the provider; the next candidate rejects the same cookie.

4. **Connect failures logged an empty reason** ("failed:  — transient, will
   retry"), because httpx leaves str(exc) empty for a refused connect.
"""

import asyncio

import pytest
from fastapi import HTTPException


# ── 1. observability must not cancel the work it observes ──────────────────
class TestForegroundGateClassification:
    @pytest.mark.parametrize("path", [
        "/api/agents/overview",
        "/api/agents/approvals",
        "/api/agents/stream",
        "/api/chat/runs",
        "/api/email/unread-state",
    ])
    def test_observability_polls_are_passive(self, path):
        """These are read-only views of background work on a client timer.
        Tracking one means a scheduled task can never outlive a poll interval."""
        from src.interactive_gate import should_track_interactive_request

        assert should_track_interactive_request(path, "GET") is False

    @pytest.mark.parametrize("path", [
        "/api/chat/stream",
        "/api/email/list",
        "/api/documents",
    ])
    def test_real_interaction_still_counts(self, path):
        """The gate exists for a reason: genuine UI work still wins."""
        from src.interactive_gate import should_track_interactive_request

        assert should_track_interactive_request(path, "GET") is True

    def test_poll_header_opts_a_read_out(self):
        """A timer-driven poll that reuses an interactive endpoint can declare
        itself, so the inbox ticker does not read as user intent."""
        from src.interactive_gate import should_track_interactive_request

        poll = {"x-odysseus-poll": "1"}
        assert should_track_interactive_request("/api/email/list", "GET", poll) is False

    def test_poll_header_never_excuses_a_write(self):
        """Nothing that mutates state is passive, whatever it claims."""
        from src.interactive_gate import should_track_interactive_request

        poll = {"x-odysseus-poll": "1"}
        assert should_track_interactive_request("/api/email/list", "POST", poll) is True

    def test_absent_or_falsy_header_is_tracked(self):
        from src.interactive_gate import should_track_interactive_request

        assert should_track_interactive_request("/api/email/list", "GET", None) is True
        assert should_track_interactive_request(
            "/api/email/list", "GET", {"x-odysseus-poll": "0"}
        ) is True

    def test_hostile_header_object_does_not_break_the_request(self):
        """The gate runs in middleware on every request; a header mapping that
        raises must not turn into a 500."""
        from src.interactive_gate import should_track_interactive_request

        class Boom:
            def get(self, _key, _default=None):
                raise RuntimeError("no headers here")

        assert should_track_interactive_request("/api/email/list", "GET", Boom()) is True


# ── 2. a delegation result must not carry the transcript twice ─────────────
class TestDelegationResultSize:
    def test_raw_stream_is_not_duplicated_alongside_the_transcript(self):
        """With a parsed envelope the raw stream-json is redundant: keep a tail
        for debugging, not the whole 50KB."""
        from src.agent_tools import claude_code_tools as cc

        assert cc.RAW_TAIL_ON_ENVELOPE < cc.MAX_OUTPUT / 10

    def test_run_result_keeps_full_output_when_there_is_no_envelope(self, monkeypatch, tmp_path):
        """The no-envelope case is exactly when raw bytes are the only evidence
        of what went wrong, so that path must keep them."""
        from src.agent_tools import claude_code_tools as cc

        raw = ("x" * 40000)
        result = self._result_for(cc, monkeypatch, tmp_path, raw_stdout=raw, envelope=None)
        assert len(result.get("output") or "") > cc.RAW_TAIL_ON_ENVELOPE

    def test_run_result_clips_output_when_an_envelope_was_parsed(self, monkeypatch, tmp_path):
        from src.agent_tools import claude_code_tools as cc

        raw = "\n".join('{"type":"assistant","message":{"content":"%d"}}' % i for i in range(2000))
        assert len(raw) > cc.MAX_OUTPUT // 2
        result = self._result_for(
            cc, monkeypatch, tmp_path, raw_stdout=raw,
            envelope={"type": "result", "result": "done"},
        )
        assert len(result.get("output") or "") <= cc.RAW_TAIL_ON_ENVELOPE
        assert result.get("output_truncated") is True
        # The structured view survives — this is a size fix, not a data loss.
        assert "transcript" in result

    @staticmethod
    def _result_for(cc, monkeypatch, tmp_path, *, raw_stdout, envelope):
        """Drive _run_claude with a fake CLI that emits `raw_stdout`."""
        binary = tmp_path / "claude"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return raw_stdout.encode(), b""

            async def wait(self):
                return 0

        monkeypatch.setattr(cc, "binary_path", lambda: binary)

        async def _info(_b):
            return {"stream_json": True, "flags": []}

        monkeypatch.setattr(cc, "binary_info", _info)
        monkeypatch.setattr(cc, "_flag_setting", lambda *_a, **_k: True)
        monkeypatch.setattr(cc, "_claude_environment", lambda: {})
        monkeypatch.setattr(cc, "_build_argv", lambda *a, **k: [str(binary)])

        async def _pump(_proc, transcript):
            transcript.envelope = envelope
            return raw_stdout.encode(), b""

        monkeypatch.setattr(cc, "_pump_stream", _pump)

        async def _noop_dict(*_a, **_k):
            return {}

        monkeypatch.setattr(cc, "_git_report", _noop_dict)
        monkeypatch.setattr(cc, "_git_changes", _noop_dict)

        async def _head(_r):
            return None

        monkeypatch.setattr(cc, "_git_head", _head)
        monkeypatch.setattr(cc, "_finish_run", lambda *_a, **_k: None)
        monkeypatch.setattr(cc, "_summarize_envelope", lambda *_a, **_k: None)

        async def _create(*_a, **_k):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
        return asyncio.run(cc._run_claude(tmp_path, "prompt", 60, []))


# ── 3. a rejected credential is terminal, not a reason to try the next model ─
class TestFallbackChainFailFast:
    @pytest.mark.parametrize("status", [400, 401, 403, 422])
    def test_auth_and_bad_request_stop_the_chain(self, status):
        from src.llm_core import _is_terminal_fallback_error

        assert _is_terminal_fallback_error(HTTPException(status, "no")) is True

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_transient_statuses_still_fall_through(self, status):
        from src.llm_core import _is_terminal_fallback_error

        assert _is_terminal_fallback_error(HTTPException(status, "later")) is False

    def test_non_http_errors_still_fall_through(self):
        from src.llm_core import _is_terminal_fallback_error

        assert _is_terminal_fallback_error(ConnectionError("refused")) is False

    def test_expired_credentials_do_not_reach_the_second_candidate(self, monkeypatch):
        """The actionable "reconnect the provider" error must survive, and the
        same rejected cookie must not be replayed at every other endpoint."""
        from src import llm_core

        tried = []

        async def _fake_call(url, model, _messages, **_kw):
            tried.append(model)
            raise HTTPException(401, "credentials expired or were rejected")

        monkeypatch.setattr(llm_core, "llm_call_async", _fake_call)

        with pytest.raises(HTTPException) as caught:
            asyncio.run(llm_core.llm_call_async_with_fallback(
                [("http://a", "primary", {}), ("http://b", "backup", {})], [],
            ))

        assert caught.value.status_code == 401
        assert tried == ["primary"]

    def test_a_transient_primary_still_fails_over(self, monkeypatch):
        from src import llm_core

        tried = []

        async def _fake_call(url, model, _messages, **_kw):
            tried.append(model)
            if model == "primary":
                raise HTTPException(503, "outage")
            return "second answer"

        monkeypatch.setattr(llm_core, "llm_call_async", _fake_call)

        got = asyncio.run(llm_core.llm_call_async_with_fallback(
            [("http://a", "primary", {}), ("http://b", "backup", {})], [],
        ))
        assert got == "second answer"
        assert tried == ["primary", "backup"]


# ── 4. logs must always name a cause ──────────────────────────────────────
class TestFailureLogging:
    def test_connect_reason_is_never_empty(self):
        """httpx leaves str() empty for a refused connect, which logged as
        "failed:  — transient, will retry"."""
        import httpx

        from src.llm_core import _connect_reason

        reason = _connect_reason(httpx.ConnectError(""))
        assert reason.strip()
        assert "ConnectError" in reason

    def test_connect_reason_prefers_the_real_message(self):
        import httpx

        from src.llm_core import _connect_reason

        assert _connect_reason(httpx.ConnectError("Network is unreachable")) == "Network is unreachable"

    def test_connect_reason_surfaces_the_cause(self):
        from src.llm_core import _connect_reason

        try:
            try:
                raise OSError("Network is unreachable")
            except OSError as inner:
                raise RuntimeError("") from inner
        except RuntimeError as exc:
            assert "Network is unreachable" in _connect_reason(exc)

    @pytest.mark.parametrize("status", [401, 403, 429, 502, 503, 504])
    def test_upstream_states_log_without_a_stack_trace(self, status):
        """These repeat once per session for as long as the condition lasts; a
        traceback each time buries real faults."""
        from src.llm_core import is_expected_upstream_failure

        assert is_expected_upstream_failure(HTTPException(status, "x")) is True

    def test_unexpected_errors_keep_their_traceback(self):
        from src.llm_core import is_expected_upstream_failure

        assert is_expected_upstream_failure(ValueError("bug")) is False
        assert is_expected_upstream_failure(HTTPException(400, "we built it wrong")) is False

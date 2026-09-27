"""Sign Claude Code in from the web UI (src/claude_code_login.py).

A stub CLI (a Python script, so it runs on Windows too) stands in for
``claude auth login``: it prints the link the real 2.1.28x command prints,
reads ``code#state`` lines from stdin, and exits 0/1 the same way.
"""
import asyncio
import json
import logging
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src import claude_code_login as ccl
from src.agent_tools import claude_code_tools as cct

GOOD_CODE = "AbCdEf0123456789goodcode#St4teValue_xyz"
BAD_CODE = "ZyXwVu9876543210wrongcode#St4teValue_xyz"
URL = "https://claude.ai/oauth/authorize?code=true&client_id=abc&response_type=code&state=St4teValue_xyz"

FAKE_CLI = r'''
import sys, time
args = sys.argv[1:]
mode, record = args[0], args[1]
cli = args[2:]
if cli[:2] == ["auth", "logout"]:
    print("Successfully logged out from your Anthropic account.")
    sys.exit(0)
if mode == "silent":
    time.sleep(60)
    sys.exit(0)
out = sys.stdout
out.write("Opening browser to sign in…\n")
if mode == "osc":
    out.write("If the browser didn't open, visit: \x1b]8;;%s\x07\x1b[94m%s\x1b[39m\x1b]8;;\x07\n" % (URL, URL))
else:
    out.write("If the browser didn't open, visit: %s\n" % URL)
out.write("Paste code here if prompted > ")
out.flush()
for line in sys.stdin:
    code = line.strip()
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(code + "\n")
    if code.startswith("AbCd"):
        out.write("Login successful.\n")
        out.flush()
        sys.exit(0)
    if code.startswith("Half"):
        sys.stderr.write("Invalid code. Please make sure the full code was copied.\n")
        sys.stderr.flush()
        continue
    sys.stderr.write("Login failed: Request failed with status code 400\n")
    sys.stderr.flush()
    sys.exit(1)
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text("URL = %r\n" % URL + FAKE_CLI, encoding="utf-8")
    record = tmp_path / "received.txt"
    state = {"mode": "normal"}
    monkeypatch.setattr(ccl, "_binary_argv", lambda: [sys.executable, str(script), state["mode"], str(record)])
    monkeypatch.setattr(ccl, "_binary_ready", lambda: None)
    monkeypatch.setattr(cct, "update_in_progress", lambda: False)
    monkeypatch.setattr(cct, "_active_task_ids", lambda: [])
    monkeypatch.setattr(cct, "_ACTIVE_RUNS", 0)
    monkeypatch.setattr(cct, "_claude_home", lambda: str(tmp_path))

    async def signed_in(binary=None):
        return {"checked": True, "logged_in": True, "auth_method": "claude.ai", "api_provider": "firstParty"}

    async def details():
        return {"email": "admin@example.com", "subscription": "max", "auth_method": "claude.ai"}

    monkeypatch.setattr(cct, "auth_status", signed_in)
    monkeypatch.setattr(ccl, "_account_details", details)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    ccl._reset_for_tests()
    yield SimpleNamespace(state=state, record=record)
    ccl._reset_for_tests()


def _no_secret(blob, *secrets):
    text = json.dumps(blob) if not isinstance(blob, str) else blob
    for secret in secrets:
        for part in (secret, *secret.split("#")[:1]):
            assert part not in text


async def _wait_state(want, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = ccl.status()
        if view["state"] in want:
            return view
        await asyncio.sleep(0.05)
    return ccl.status()


# ── the flow ──

async def test_url_captured_code_delivered_and_signed_in(fake_cli, caplog):
    caplog.set_level(logging.DEBUG)
    started = await ccl.start("claudeai")
    assert started["state"] == "awaiting_code"
    assert started["url"] == URL
    assert started["expires_in"] > 500

    done = await ccl.submit_code(started["session_id"], GOOD_CODE)
    assert done["state"] == "done", done
    assert done["account"]["email"] == "admin@example.com"
    assert "url" not in done
    # The stub received exactly the code on its stdin.
    assert fake_cli.record.read_text(encoding="utf-8").splitlines() == [GOOD_CODE]
    # Never in a response, the status view, or the log.
    _no_secret(done, GOOD_CODE)
    _no_secret(ccl.status(), GOOD_CODE)
    _no_secret(caplog.text, GOOD_CODE)
    assert "starting -> awaiting_code" in caplog.text
    assert "verifying -> done" in caplog.text
    assert URL not in caplog.text


async def test_osc8_hyperlink_and_ansi_are_stripped_from_the_url(fake_cli):
    fake_cli.state["mode"] = "osc"
    started = await ccl.start()
    assert started["url"] == URL
    await ccl.cancel(started["session_id"])


async def test_cli_failure_reports_its_message_without_the_code(fake_cli, caplog):
    caplog.set_level(logging.DEBUG)
    started = await ccl.start()
    result = await ccl.submit_code(started["session_id"], BAD_CODE)
    assert result["state"] == "failed"
    assert "Login failed: Request failed with status code 400" in result["error"]
    _no_secret(result, BAD_CODE)
    _no_secret(caplog.text, BAD_CODE)
    assert ccl._SESSION.proc.returncode == 1


async def test_cli_rejecting_the_code_returns_to_awaiting_code(fake_cli):
    started = await ccl.start()
    result = await ccl.submit_code(started["session_id"], "Half0123456789abc#St4te")
    assert result["state"] == "awaiting_code"
    assert "rejected" in result["error"]
    # The process is still waiting; a good code then completes the sign-in.
    done = await ccl.submit_code(started["session_id"], GOOD_CODE)
    assert done["state"] == "done"


async def test_malformed_code_never_reaches_the_process(fake_cli):
    started = await ccl.start()
    for code in ("no-hash-here", "abc#def\nsecond-line", "", "a b#c"):
        with pytest.raises(ccl.LoginError) as exc:
            await ccl.submit_code(started["session_id"], code)
        assert exc.value.status == 400
    assert ccl.status()["state"] == "awaiting_code"
    assert not fake_cli.record.exists()
    await ccl.cancel()


async def test_wrong_session_id_is_refused(fake_cli):
    await ccl.start()
    with pytest.raises(ccl.LoginError) as exc:
        await ccl.submit_code("not-the-session", GOOD_CODE)
    assert exc.value.status == 404
    await ccl.cancel()


async def test_expiry_kills_the_process(fake_cli, monkeypatch):
    monkeypatch.setattr(ccl, "SESSION_TTL_S", 1)
    started = await ccl.start()
    assert started["state"] == "awaiting_code"
    proc = ccl._SESSION.proc
    view = await _wait_state({"expired"}, timeout=10)
    assert view["state"] == "expired"
    for _ in range(100):
        if proc.returncode is not None:
            break
        await asyncio.sleep(0.05)
    assert proc.returncode is not None
    with pytest.raises(ccl.LoginError):
        await ccl.submit_code(started["session_id"], GOOD_CODE)


async def test_cancel_kills_the_process(fake_cli):
    started = await ccl.start()
    proc = ccl._SESSION.proc
    view = await ccl.cancel(started["session_id"])
    assert view["state"] == "cancelled"
    assert proc.returncode is not None


async def test_no_link_within_the_wait_fails_and_kills(fake_cli, monkeypatch):
    fake_cli.state["mode"] = "silent"
    monkeypatch.setattr(ccl, "URL_WAIT_S", 1)
    view = await ccl.start()
    assert view["state"] == "failed"
    assert "did not print a sign-in link" in view["error"]
    assert ccl._SESSION.proc.returncode is not None


async def test_start_is_rate_limited(fake_cli):
    await ccl.start()
    with pytest.raises(ccl.LoginError) as exc:
        await ccl.start()
    assert exc.value.status == 429
    await ccl.cancel()


async def test_new_start_replaces_the_previous_session(fake_cli, monkeypatch):
    first = await ccl.start()
    old_proc = ccl._SESSION.proc
    monkeypatch.setattr(ccl, "_LAST_START", 0.0)
    second = await ccl.start()
    assert second["session_id"] != first["session_id"]
    assert old_proc.returncode is not None
    await ccl.cancel()


async def test_shutdown_kills_a_pending_login(fake_cli):
    await ccl.start()
    proc = ccl._SESSION.proc
    await ccl.ashutdown()
    assert proc.returncode is not None
    assert ccl.status()["state"] == "cancelled"


async def test_stored_credentials_overridden_by_env_are_named_not_shown(fake_cli, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-supersecretvalue-1234567890")
    started = await ccl.start()
    done = await ccl.submit_code(started["session_id"], GOOD_CODE)
    assert "ANTHROPIC_API_KEY" in done["message"]
    assert "supersecretvalue" not in json.dumps(done)
    assert ccl.status()["overriding_credentials"] == ["ANTHROPIC_API_KEY"]


def test_login_environment_is_the_delegation_allowlist(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_shouldneverleak0000000000000000000")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "refresh-should-not-pass")
    monkeypatch.setattr(cct, "_claude_home", lambda: str(tmp_path))
    env = ccl._login_environment()
    assert env["HOME"] == str(tmp_path)
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env
    assert "ODYSSEUS_API_TOKEN" not in env


# ── logout ──

async def test_logout_refused_while_a_delegation_runs(fake_cli, monkeypatch):
    monkeypatch.setattr(cct, "_ACTIVE_RUNS", 1)
    with pytest.raises(ccl.LoginError) as exc:
        await ccl.logout()
    assert exc.value.status == 409


async def test_logout_runs_the_cli_and_reports_status(fake_cli, monkeypatch):
    async def signed_out(binary=None):
        return {"checked": True, "logged_in": False, "auth_method": "none"}

    monkeypatch.setattr(cct, "auth_status", signed_out)
    result = await ccl.logout()
    assert result["exit_code"] == 0
    assert result["logged_in"] is False
    assert "logged out" in result["message"]


# ── routes: admin browser session only ──

def _endpoint(path, method):
    from routes.claude_code_routes import setup_claude_code_routes
    for route in setup_claude_code_routes().routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} is not registered")


def _cookie_request(*, is_admin=True, headers=None):
    auth_mgr = SimpleNamespace(is_configured=True, is_admin=lambda user: is_admin)
    return SimpleNamespace(state=SimpleNamespace(current_user="bob", api_token=False),
                           app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_mgr)),
                           headers=headers or {}, client=SimpleNamespace(host="203.0.113.5"))


def _token_request():
    return SimpleNamespace(
        state=SimpleNamespace(current_user="api", api_token=True, api_token_scopes=["claude_code:write"],
                              api_token_owner="alice"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)), headers={},
        client=SimpleNamespace(host="203.0.113.5"))


_ROUTES = [
    ("/api/claude-code/login/status", "GET", ()),
    ("/api/claude-code/login/start", "POST", ({},)),
    ("/api/claude-code/login/code", "POST", ({"session_id": "x", "code": GOOD_CODE},)),
    ("/api/claude-code/login/cancel", "POST", ({},)),
    ("/api/claude-code/logout", "POST", ()),
]


@pytest.mark.parametrize("path,method,args", _ROUTES)
async def test_routes_refuse_api_tokens_even_with_write_scope(monkeypatch, path, method, args):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    with pytest.raises(HTTPException) as exc:
        await _endpoint(path, method)(_token_request(), *args)
    assert exc.value.status_code == 403
    assert "API token" in exc.value.detail


@pytest.mark.parametrize("path,method,args", _ROUTES)
async def test_routes_refuse_the_internal_agent_token(monkeypatch, path, method, args):
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN
    monkeypatch.setenv("AUTH_ENABLED", "true")
    request = _cookie_request(headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN})
    with pytest.raises(HTTPException) as exc:
        await _endpoint(path, method)(request, *args)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("path,method,args", _ROUTES)
async def test_routes_refuse_non_admins(monkeypatch, path, method, args):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    with pytest.raises(HTTPException) as exc:
        await _endpoint(path, method)(_cookie_request(is_admin=False), *args)
    assert exc.value.status_code in (401, 403)


async def test_admin_route_flow_never_echoes_the_code(fake_cli, monkeypatch, caplog):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    caplog.set_level(logging.DEBUG)
    req = _cookie_request()
    assert (await _endpoint("/api/claude-code/login/status", "GET")(req))["state"] == "idle"
    started = await _endpoint("/api/claude-code/login/start", "POST")(req, {"method": "claudeai"})
    assert started["url"] == URL
    with pytest.raises(HTTPException) as exc:
        await _endpoint("/api/claude-code/login/code", "POST")(req, {"session_id": started["session_id"],
                                                                    "code": "missing-the-hash"})
    assert exc.value.status_code == 400
    assert "missing-the-hash" not in exc.value.detail
    done = await _endpoint("/api/claude-code/login/code", "POST")(
        req, {"session_id": started["session_id"], "code": GOOD_CODE})
    assert done["state"] == "done"
    _no_secret(done, GOOD_CODE)
    _no_secret(caplog.text, GOOD_CODE)


async def test_start_route_maps_rate_limit_to_429(fake_cli, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    req = _cookie_request()
    await _endpoint("/api/claude-code/login/start", "POST")(req, {})
    with pytest.raises(HTTPException) as exc:
        await _endpoint("/api/claude-code/login/start", "POST")(req, {})
    assert exc.value.status_code == 429
    await _endpoint("/api/claude-code/login/cancel", "POST")(req, {})


def test_no_agent_tool_can_submit_a_code():
    """The chat tool has no login action; only the admin UI drives this."""
    assert "login" not in cct._ACTIONS
    import inspect
    source = inspect.getsource(cct)
    assert "claude_code_login" not in source


async def test_not_signed_in_hint_points_to_settings(monkeypatch, tmp_path):
    async def info(binary=None):
        return {"path": "/app/data/claude", "available": True, "version": "2.1.280", "error": "",
                "flags": ["--permission-prompts", "--restricted"]}

    async def signed_out(binary=None):
        return {"checked": True, "logged_in": False, "auth_method": "none"}

    monkeypatch.setattr(cct, "binary_info", info)
    monkeypatch.setattr(cct, "auth_status", signed_out)
    monkeypatch.setattr(cct, "repository_roots", lambda: (tmp_path,))
    report = await cct.status_report()
    hint = next(h for h in report["hints"] if "not signed in" in h)
    assert "Settings > Tools > Claude Code delegation > Sign in" in hint
    assert "agent cannot do this step" in hint

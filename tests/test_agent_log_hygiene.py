"""Log hygiene and cache affinity fixes from the 2026-09-11 agent logs.

The pasted log of one 21-round turn showed:

* `bash: set -e -> exit_code=1` — a multi-line script previewed by its first
  line, so the failing command was invisible.
* `delegate_to_claude_code -> exit_code=1` — a failed tool call with no reason
  in the log.
* a bash preview cut off one character before a bearer token — the 80-char
  limit was the only thing keeping the secret out of the log file.
* the identical `[agent-debug] ... schema_without_selection=[...]` line on
  every round, burying the round that mattered.
* `selected_without_schema=['generate_image']` on every round with image
  generation switched off.
* prompt cache hits bouncing 4608 -> 14848 -> 0 -> 14848 -> 4608 -> 18944 on
  consecutive rounds of one conversation: no shard affinity.
"""
import inspect
import re
import uuid

import pytest

from src.tool_execution import _command_preview, _failure_detail


# ── bash / script previews ──

def test_preview_skips_shell_boilerplate_and_counts_the_rest():
    script = "set -e\n\n# push the branch\ncd /app/repo && git push -u origin feature\necho done\n"
    assert _command_preview(script) == "cd /app/repo && git push -u origin feature (+1 lines)"


def test_preview_skips_set_euo_pipefail_and_shebang_comments():
    script = "#!/usr/bin/env bash\nset -euo pipefail\nset -o pipefail\ngit status --short"
    assert _command_preview(script) == "git status --short"


def test_preview_falls_back_to_the_first_line_when_everything_is_boilerplate():
    assert _command_preview("set -e\n# nothing else") == "set -e (+1 lines)"
    assert _command_preview("") == ""
    assert _command_preview(None) == ""


def test_preview_truncates_to_the_limit():
    long = "echo " + "x" * 200
    out = _command_preview(long, 60)
    assert out.startswith("echo xxxx")
    assert len(out) == 60


def test_preview_redacts_bearer_tokens_and_provider_keys():
    token = "ghp_" + "A" * 36
    script = f'api=https://api.github.com/repos/o/r; auth="Authorization: Bearer {token}"; curl -H "$auth" $api'
    out = _command_preview(script, 200)
    assert token not in out
    assert "ghp_" not in out
    assert out.startswith("api=https://api.github.com/repos/o/r")

    key = "sk-" + "b" * 40
    assert key not in _command_preview(f"export OPENAI_API_KEY={key}\npython run.py", 200)


def test_preview_keeps_variable_references_readable():
    # `$GITHUB_TOKEN` is not a secret; the preview must still say which var was used.
    out = _command_preview('if [ -n "${GITHUB_PERSONAL_ACCESS_TOKEN:-}" ]; then echo present; fi')
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in out


# ── failure detail on the `Tool executed` line ──

def test_failure_detail_surfaces_the_error_field():
    detail = _failure_detail({"error": "delegate_to_claude_code: repository is not approved. Candidates: /app/x", "exit_code": 1})
    assert detail.startswith("delegate_to_claude_code: repository is not approved")


def test_failure_detail_uses_the_output_tail_for_nonzero_exit():
    out = "lots of progress\n" * 50 + "fatal: not a git repository (or any of the parent directories): .git"
    detail = _failure_detail({"output": out, "exit_code": 128})
    assert "fatal: not a git repository" in detail
    assert "\n" not in detail
    assert len(detail) <= 201


def test_failure_detail_is_empty_on_success_and_non_dicts():
    assert _failure_detail({"output": "ok", "exit_code": 0}) == ""
    assert _failure_detail({"output": "ok"}) == ""
    assert _failure_detail({"result": "fine", "exit_code": "n/a"}) == ""
    assert _failure_detail("string result") == ""
    assert _failure_detail(None) == ""


def test_failure_detail_redacts_secrets_and_collapses_whitespace():
    token = "github_pat_" + "Z" * 40
    detail = _failure_detail({"error": f"curl failed:\n  Authorization: Bearer {token}\n  401", "exit_code": 1})
    assert token not in detail
    assert "\n" not in detail


def test_tool_executed_log_line_carries_the_failure_reason():
    import src.tool_execution as te
    src = inspect.getsource(te)
    assert '"Tool executed: %s -> exit_code=%s%s"' in src
    assert 'f" error={_detail}" if _detail else ""' in src


# ── per-round tool-set diff logging ──

def test_tool_set_diff_line_is_info_only_when_the_set_changes():
    from src import agent_loop
    src = inspect.getsource(agent_loop.stream_agent_loop)
    assert "_last_tool_debug_sig = None" in src
    assert "_tool_debug_log = logger.info if _tool_debug_sig != _last_tool_debug_sig else logger.debug" in src
    # The old unconditional INFO must be gone.
    assert re.search(r'logger\.info\(\s*"\[agent-debug\] round=%s model=%s', src) is None


def test_image_generation_off_disables_generate_image_in_the_selection():
    from src import agent_loop
    src = inspect.getsource(agent_loop.stream_agent_loop)
    assert 'if not get_setting("image_gen_enabled", False):\n        disabled_tools.add("generate_image")' in src
    assert "_relevant_tools = set(_relevant_tools) - disabled_tools" in src


# ── Codex cache-shard affinity ──

def test_affinity_headers_are_uuid_shaped_and_stable_per_session(monkeypatch):
    from src.llm_core import _chatgpt_affinity_headers

    monkeypatch.setattr("src.llm_core._responses_prompt_cache_key_enabled", lambda: True)
    a = _chatgpt_affinity_headers({"Content-Type": "application/json"}, "session-abc")
    b = _chatgpt_affinity_headers({}, "session-abc")
    other = _chatgpt_affinity_headers({}, "session-xyz")

    assert a["session_id"] == a["conversation_id"] == b["session_id"]
    assert str(uuid.UUID(a["session_id"])) == a["session_id"]
    assert other["session_id"] != a["session_id"]
    assert a["Content-Type"] == "application/json"


def test_affinity_headers_respect_the_cache_key_kill_switch_and_missing_session(monkeypatch):
    from src.llm_core import _chatgpt_affinity_headers

    monkeypatch.setattr("src.llm_core._responses_prompt_cache_key_enabled", lambda: True)
    assert "session_id" not in _chatgpt_affinity_headers({}, None)
    assert "session_id" not in _chatgpt_affinity_headers({}, "")
    monkeypatch.setattr("src.llm_core._responses_prompt_cache_key_enabled", lambda: False)
    assert "session_id" not in _chatgpt_affinity_headers({}, "session-abc")


def test_affinity_headers_do_not_overwrite_caller_headers(monkeypatch):
    from src.llm_core import _chatgpt_affinity_headers

    monkeypatch.setattr("src.llm_core._responses_prompt_cache_key_enabled", lambda: True)
    h = _chatgpt_affinity_headers({"session_id": "keep-me"}, "session-abc")
    assert h["session_id"] == "keep-me"
    assert "conversation_id" in h


def test_streaming_codex_requests_send_the_affinity_headers():
    from src import llm_core
    src = inspect.getsource(llm_core._stream_llm_inner)
    assert "h = _chatgpt_affinity_headers(_provider_headers(provider, headers), session_id)" in src

"""prompt_cache_key is shared by sessions with the same prefix (2026-10-02)."""
import pytest

from src import llm_core
from src.llm_core import _build_chatgpt_responses_payload

TOOL_A = {"type": "function", "function": {"name": "grep", "description": "d", "parameters": {"type": "object", "properties": {}}}}
TOOL_B = {"type": "function", "function": {"name": "bash", "description": "d", "parameters": {"type": "object", "properties": {}}}}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    llm_core._SESSION_CACHE_KEYS.clear()
    monkeypatch.setattr("src.llm_core._shared_prompt_cache_key_enabled", lambda: True)
    monkeypatch.setattr("src.llm_core._responses_prompt_cache_key_enabled", lambda: True)
    yield
    llm_core._SESSION_CACHE_KEYS.clear()


def _key(session, system="sys", tools=(TOOL_A,), model="gpt-6-sol", scope=""):
    return _build_chatgpt_responses_payload(
        model, [{"role": "system", "content": system}, {"role": "user", "content": "hi"}],
        0.7, 100, stream=True, tools=list(tools), cache_key=session, cache_scope=scope,
    )["prompt_cache_key"]


def test_same_prefix_shares_key_across_sessions():
    assert _key("s1") == _key("s2")


def test_session_keeps_key_when_tools_or_instructions_change():
    first = _key("s1")
    assert _key("s1", tools=(TOOL_A, TOOL_B)) == first
    assert _key("s1", system="other") == first


def test_different_prefix_gives_different_key():
    assert _key("s1", system="a") != _key("s2", system="b")
    assert _key("s3", tools=(TOOL_A,)) != _key("s4", tools=(TOOL_B,))
    assert _key("s5", model="m1") != _key("s6", model="m2")


def test_accounts_never_share_key():
    assert _key("s1", scope="acct-1") != _key("s2", scope="acct-2")


def test_setting_off_restores_session_id(monkeypatch):
    monkeypatch.setattr("src.llm_core._shared_prompt_cache_key_enabled", lambda: False)
    assert _key("s1") == "s1"


def test_memory_is_bounded(monkeypatch):
    monkeypatch.setattr("src.llm_core._SESSION_CACHE_KEYS_MAX", 3)
    for i in range(10):
        _key(f"s{i}", system=f"sys{i}")
    assert len(llm_core._SESSION_CACHE_KEYS) == 3

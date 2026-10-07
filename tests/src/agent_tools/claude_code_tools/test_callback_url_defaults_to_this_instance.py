"""A delegated Claude Code session calls back into this instance with only a token configured.

The callback needed two settings: a token file and the URL of this very
instance. A local child runs beside the app, so the URL is the app's own
loopback address; an install that set only the token got no callback at all,
with nothing saying why.
"""
import pytest

from src.agent_tools import claude_code_tools as cct

pytestmark = pytest.mark.security


@pytest.fixture
def settings(monkeypatch):
    store = {}
    monkeypatch.setattr(cct, "_setting", lambda key, default=None: (
        default if key not in store or store[key] in (None, "", [], {}) else store[key]))
    monkeypatch.delenv("CLAUDE_CODE_ODYSSEUS_URL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ODYSSEUS_TOKEN_FILE", raising=False)
    monkeypatch.delenv("ODYSSEUS_INTERNAL_BASE", raising=False)
    return store


def test_a_token_alone_enables_the_callback_to_this_instance(settings, monkeypatch):
    monkeypatch.setenv("APP_PORT", "7860")
    settings["claude_code_odysseus_token_file"] = "/app/data/claude-agent.token"
    callback = cct._callback_config()
    assert callback == {"url": "http://127.0.0.1:7860", "token_file": "/app/data/claude-agent.token",
                        "enabled": True}


def test_an_explicit_url_still_wins(settings):
    settings["claude_code_odysseus_token_file"] = "/app/data/claude-agent.token"
    settings["claude_code_odysseus_url"] = "http://agamemnon.lan:7000"
    assert cct._callback_config()["url"] == "http://agamemnon.lan:7000"


def test_no_token_means_no_callback(settings):
    callback = cct._callback_config()
    assert callback["enabled"] is False and callback["url"] == ""

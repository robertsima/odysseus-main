"""manage_settings accepts the everyday names for the input-token ceiling.

A person asks the agent to "raise the hard max" or the "token budget cap";
without the alias the call is refused as an unknown setting and nothing changes.
"""
import asyncio
import json

import pytest

import src.settings as settings_mod
from src.agent_tools.admin_tools import do_manage_settings


@pytest.mark.parametrize("name", ["hard max", "token budget cap", "input budget cap", "Token Budget Cap"])
def test_a_friendly_name_sets_the_input_token_ceiling(monkeypatch, name):
    store = {}
    monkeypatch.setattr(settings_mod, "load_settings", lambda: dict(store))
    monkeypatch.setattr(settings_mod, "save_settings", lambda s: store.update(s))

    result = asyncio.run(do_manage_settings(json.dumps({"action": "set", "key": name, "value": 200000})))

    assert result.get("exit_code") == 0, result
    assert store.get("agent_input_token_hard_max") == 200000

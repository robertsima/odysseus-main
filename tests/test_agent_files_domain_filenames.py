"""Naming concrete files must select file tools.

"add the marker token SCANPROOF to models.md and architecture.md in my AI Mind
vault" matched no domain: the files regex looks for the words file/folder/repo/
shell, none of which appear. With domains empty, tool selection fell through to
pure embedding retrieval, which collided with the query's own nouns — "token"
retrieved manage_tokens, "models.md" retrieved list_models/serve_model — so the
agent was offered no file tool at all and correctly reported it could not write.

Same shape as #3794 (api_call), and fixed the same way: seed the domain
deterministically rather than trusting retrieval.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from src.agent_loop import _DOMAIN_TOOL_MAP, _classify_agent_request


def _domains(text):
    return _classify_agent_request([{"role": "user", "content": text}], text)["domains"]


REPRO = "add the marker token SCANPROOF to models.md and architecture.md in my AI Mind vault"


def test_the_original_repro_now_selects_file_tools():
    assert "files" in _domains(REPRO)


def test_repro_domain_maps_to_write_tools():
    """Selecting the domain is only useful if it carries the editing tools."""
    tools = _DOMAIN_TOOL_MAP["files"]
    assert {"read_file", "edit_file", "apply_patch"} <= tools


@pytest.mark.parametrize("text", [
    "update notes.md",
    "add a line to config.yaml",
    "read architecture.md",
    "what changed in main.py",
    "append to data.json",
    "check settings.toml",
    "fix the bug in app.ts",
    "look at my vault",
    "put this in the vault",
    "open /app/data/personal_docs/note.md",
    "check ~/projects/thing/",
])
def test_filenames_paths_and_vaults_select_files(text):
    assert "files" in _domains(text), text


@pytest.mark.parametrize("text", [
    "what is the weather today",
    "send an email to chris",
    "remind me to buy milk",
    "who am I",
    "what models are running",
])
def test_unrelated_requests_do_not_select_files(text):
    assert "files" not in _domains(text), text


@pytest.mark.parametrize("text", [
    "version 1.2.3 of the app",
    "it costs 3.50",
])
def test_dotted_non_filenames_do_not_select_files(text):
    """The extension list keeps this precise — version and decimal numbers must
    not be read as filenames."""
    assert "files" not in _domains(text), text


def test_naming_a_file_is_no_longer_low_signal():
    """low_signal is `not continuation and not domains`, so seeding the domain
    also keeps the turn off the low-signal path that strips write tools."""
    intent = _classify_agent_request([{"role": "user", "content": REPRO}], REPRO)
    assert intent["low_signal"] is False

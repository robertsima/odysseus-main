"""Search provider names the settings store holds, as the dispatcher reads them.

Two 2026-09-28 findings. The Research tab saved "google", which matched no
provider in the dispatcher (it knows "google_pse"), so Deep Research silently
used the fallback engine. And removing the last fallback provider in Settings
saved [], which the chain reads as "the default fallback" — there was no way
to say "no fallback at all". ["none"] now says it; [] keeps its meaning.
"""

import asyncio
import sys
import types

import pytest

from services.search import core


@pytest.fixture
def search_settings(monkeypatch):
    settings = {"search_provider": "searxng"}
    monkeypatch.setattr(core, "_get_search_settings", lambda: settings)
    return settings


def test_an_empty_fallback_chain_still_means_the_default(search_settings):
    search_settings["search_fallback_chain"] = []
    assert core._build_provider_chain("searxng") == ["searxng", "duckduckgo"]


def test_none_means_no_fallback_at_all(search_settings):
    search_settings["search_fallback_chain"] = ["none"]
    assert core._build_provider_chain("searxng") == ["searxng"]
    # Older saves spelled it "disabled"; that keeps working.
    search_settings["search_fallback_chain"] = ["disabled"]
    assert core._build_provider_chain("searxng") == ["searxng"]


def test_google_is_read_as_google_pse(search_settings):
    search_settings["search_fallback_chain"] = ["google"]
    assert core._build_provider_chain("brave") == ["brave", "google_pse"]
    search_settings["search_fallback_chain"] = []
    assert core._build_provider_chain("google") == ["google_pse", "duckduckgo"]
    assert core.normalize_provider_name(" Google ") == "google_pse"
    assert core.normalize_provider_name(None) == ""


def test_deep_research_asks_google_pse_for_a_stored_google(monkeypatch):
    """The research provider read at run time, not only at save time: a
    settings.json written before the fix still says "google"."""
    from src.deep_research import DeepResearcher

    asked = []
    providers_mod = types.ModuleType("src.search.providers")
    providers_mod._get_search_settings = lambda: {
        "search_provider": "searxng", "research_search_provider": "google",
    }
    core_mod = types.ModuleType("src.search.core")

    def chain(provider):
        asked.append(provider)
        return [provider]

    core_mod._build_provider_chain = chain
    core_mod._call_provider = lambda prov, query, n: [{"url": "https://example.com", "title": prov}]
    monkeypatch.setitem(sys.modules, "src.search.providers", providers_mod)
    monkeypatch.setitem(sys.modules, "src.search.core", core_mod)

    researcher = DeepResearcher.__new__(DeepResearcher)
    researcher.search_provider_override = None
    researcher.providers_used = []
    results = asyncio.run(researcher._search("anything"))

    assert asked == ["google_pse"]
    assert results and researcher.providers_used == ["google_pse"]

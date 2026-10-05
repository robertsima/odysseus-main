"""After the model judged a conversation, the regex fallback may only add identity facts.

The extractor keeps a regex pass for facts the model skipped. When the model
did run and returned nothing, a brainstorm line such as "I like Umni" matched
that regex and was stored as the lasting preference "User prefers Umni". Only
identity statements ("my name is ...") survive once the model has run; if the
model call itself fails, the regex facts are all there is, so they are kept.
"""
import asyncio

import pytest

import src.event_bus
import src.llm_core
from services.memory.memory_extractor import extract_and_store
from src.memory import MemoryManager


class _Session:
    owner = "alice"
    session_id = "sess-1"

    def get_context_messages(self):
        return [
            {"role": "user", "content": "My name is Dana. Naming brainstorm: I like Umni for the app."},
            {"role": "assistant", "content": "Umni is short and easy to say."},
        ]


def _extract(monkeypatch, tmp_path, model_answer):
    async def model(url, model_id, messages, **kwargs):
        if isinstance(model_answer, Exception):
            raise model_answer
        return model_answer

    monkeypatch.setattr(src.llm_core, "llm_call_async", model)
    monkeypatch.setattr(src.event_bus, "fire_event", lambda *a, **k: None)
    manager = MemoryManager(str(tmp_path))
    asyncio.run(extract_and_store(_Session(), manager, None, endpoint_url="http://x", model="m", headers=None))
    return [m["text"] for m in manager.load(owner="alice")]


def test_once_the_model_ran_only_identity_facts_come_from_the_regex(monkeypatch, tmp_path):
    stored = _extract(monkeypatch, tmp_path, "[]")

    assert any("Dana" in text for text in stored)
    assert not any("Umni" in text for text in stored)


def test_when_the_model_call_fails_every_regex_fact_is_kept(monkeypatch, tmp_path):
    stored = _extract(monkeypatch, tmp_path, RuntimeError("model down"))

    assert any("Dana" in text for text in stored)
    assert any("Umni" in text for text in stored)

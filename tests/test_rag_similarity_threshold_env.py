"""RAG_SIMILARITY_THRESHOLD must be configurable per deployment.

The value was hardcoded at 0.35, so self-hosters who set the env var (a large
vault of short notes scores lower than the default assumes) got no effect and
no warning. Bad values fall back to the default rather than silently disabling
retrieval (0 injects everything) or blocking it (>1 injects nothing).
"""
import importlib
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import src.chat_processor as chat_processor


def _threshold_with(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("RAG_SIMILARITY_THRESHOLD", raising=False)
    else:
        monkeypatch.setenv("RAG_SIMILARITY_THRESHOLD", value)
    return chat_processor._env_rag_threshold()


def test_default_when_unset(monkeypatch):
    assert _threshold_with(monkeypatch, None) == chat_processor.DEFAULT_RAG_SIMILARITY_THRESHOLD


@pytest.mark.parametrize("raw,expected", [("0.20", 0.20), ("0", 0.0), ("1", 1.0), (" 0.5 ", 0.5)])
def test_valid_values_are_honoured(monkeypatch, raw, expected):
    assert _threshold_with(monkeypatch, raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "   ", "abc", "0.2.1", "-0.1", "1.5", "nan"])
def test_bad_values_fall_back_to_default(monkeypatch, raw):
    assert _threshold_with(monkeypatch, raw) == chat_processor.DEFAULT_RAG_SIMILARITY_THRESHOLD


def test_class_attribute_picks_up_the_env_on_import(monkeypatch):
    """The threshold is read at import and stays a plain class attribute, so
    `self.RAG_SIMILARITY_THRESHOLD` reads and tests that patch it keep working."""
    monkeypatch.setenv("RAG_SIMILARITY_THRESHOLD", "0.20")
    reloaded = importlib.reload(chat_processor)
    try:
        assert reloaded.ChatProcessor.RAG_SIMILARITY_THRESHOLD == pytest.approx(0.20)
    finally:
        monkeypatch.delenv("RAG_SIMILARITY_THRESHOLD", raising=False)
        importlib.reload(chat_processor)

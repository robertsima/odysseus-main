"""Document retrieval does not run for turns with no content in them.

The personal-document vector search is two ChromaDB collections and four round
trips. It ran unconditionally — including for "hey", "lol" and "thanks!" —
spending that latency to inject whatever a greeting happened to sit nearest in
embedding space. Meanwhile *tool* retrieval was skipped for substantive
requests. The two retrieval systems applied opposite policies to the same
signal; this is the document half.
"""

import pytest

from src.agent_loop import _is_casual_low_signal


@pytest.mark.parametrize("text", ["hey", "hi", "yo", "lol", "haha", "thanks!", "thank you"])
def test_contentless_turns_are_recognised(text):
    assert _is_casual_low_signal(text), text


@pytest.mark.parametrize("text", ["ok", "cool", "nice", "sounds good", "ok cool"])
def test_bare_acknowledgements_still_retrieve(text):
    """These read as contentless but follow a proposal often enough ("ok" =
    "do it") that the shipped classifier does not treat them as chit-chat.
    Pinned so the gate's blast radius stays visible: this fix must not quietly
    start skipping retrieval for a turn that means "go ahead"."""
    assert not _is_casual_low_signal(text), text


@pytest.mark.parametrize("text", [
    "analyze your own logs and fix this issue",
    "debug this crash",
    "fix the failing test",
    "the deploy is failing again",
    "but u have all of that",
    "yeah do that",
])
def test_substantive_turns_still_retrieve(text):
    """The narrow chit-chat test, not the broad low_signal flag: that flag is
    true for most of these, and skipping document retrieval for them would be
    the opposite of the problem being fixed."""
    assert not _is_casual_low_signal(text), text


def test_the_processor_gates_on_the_narrow_test():
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "src" / "chat_processor.py").read_text()
    gate = src.split("# RAG: search if enabled", 1)[1].split("if use_rag:", 1)[0]
    assert "_is_casual_low_signal" in gate
    # The broad flag must not be what gates document retrieval.
    assert "low_signal\"" not in gate and "intent[" not in gate

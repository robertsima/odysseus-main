from unittest.mock import MagicMock
from types import SimpleNamespace
from src.chat_processor import ChatProcessor

def test_build_context_preface_web_search_success(monkeypatch):
    """Test that LLM correctly extracts and uses a web search query."""
    mock_llm_call = MagicMock(return_value="extracted query")
    monkeypatch.setattr("src.llm_core.llm_call", mock_llm_call)

    mock_web_search = MagicMock(return_value=("Search Results", [{"url": "http://mock.com"}]))
    monkeypatch.setattr("src.chat_processor.comprehensive_web_search", mock_web_search)

    processor = ChatProcessor(memory_manager=MagicMock(), personal_docs_manager=MagicMock())
    session = SimpleNamespace(endpoint_url="http://local", model="test", headers={})

    processor.build_context_preface(
        message="Some text.\n\nSearch for LLMs.",
        session=session,
        use_web=True,
        use_rag=False,
        use_memory=False,
        use_skills=False
    )

    mock_web_search.assert_called_with("extracted query", time_filter=None, return_sources=True)

def test_build_context_preface_web_search_fallback_on_llm_failure(monkeypatch):
    """Test fallback to original query if LLM fails."""
    def failing_llm(*args, **kwargs):
        raise ValueError("LLM down")
    monkeypatch.setattr("src.llm_core.llm_call", failing_llm)

    mock_web_search = MagicMock(return_value=("Search Results", []))
    monkeypatch.setattr("src.chat_processor.comprehensive_web_search", mock_web_search)

    processor = ChatProcessor(memory_manager=MagicMock(), personal_docs_manager=MagicMock())
    session = SimpleNamespace(endpoint_url="http://local", model="test", headers={})

    processor.build_context_preface(
        message="First line\nSecond line",
        session=session,
        use_web=True,
        use_rag=False,
        use_memory=False,
        use_skills=False
    )

    mock_web_search.assert_called_with("First line", time_filter=None, return_sources=True)

def test_build_context_preface_web_search_fallback_on_empty_generation(monkeypatch):
    """Test fallback to original query if LLM returns empty string."""
    mock_llm_call = MagicMock(return_value="   \n  ")
    monkeypatch.setattr("src.llm_core.llm_call", mock_llm_call)

    mock_web_search = MagicMock(return_value=("Search Results", []))
    monkeypatch.setattr("src.chat_processor.comprehensive_web_search", mock_web_search)

    processor = ChatProcessor(memory_manager=MagicMock(), personal_docs_manager=MagicMock())
    session = SimpleNamespace(endpoint_url="http://local", model="test", headers={})

    processor.build_context_preface(
        message="\n\nFallback line\nNext",
        session=session,
        use_web=True,
        use_rag=False,
        use_memory=False,
        use_skills=False
    )

    mock_web_search.assert_called_with("Fallback line", time_filter=None, return_sources=True)

def test_build_context_preface_web_search_query_sanitization(monkeypatch):
    """Test that query is truncated and whitespace collapsed."""
    long_query = "word  " * 50
    mock_llm_call = MagicMock(return_value=long_query)
    monkeypatch.setattr("src.llm_core.llm_call", mock_llm_call)

    mock_web_search = MagicMock(return_value=("Search Results", []))
    monkeypatch.setattr("src.chat_processor.comprehensive_web_search", mock_web_search)

    processor = ChatProcessor(memory_manager=MagicMock(), personal_docs_manager=MagicMock())
    session = SimpleNamespace(endpoint_url="http://local", model="test", headers={})

    processor.build_context_preface(
        message="Message",
        session=session,
        use_web=True,
        use_rag=False,
        use_memory=False,
        use_skills=False
    )

    called_query = mock_web_search.call_args[0][0]
    assert len(called_query) <= 150
    assert "  " not in called_query


def test_query_writer_gets_the_date_and_the_recent_turns_and_room_to_answer(monkeypatch):
    """A4-14: "what about the price?" needs the last exchange, "latest" needs today."""
    mock_llm_call = MagicMock(return_value="<think>hmm</think>\nacme widget price 2026")
    monkeypatch.setattr("src.llm_core.llm_call", mock_llm_call)
    mock_web_search = MagicMock(return_value=("Search Results", []))
    monkeypatch.setattr("src.chat_processor.comprehensive_web_search", mock_web_search)

    processor = ChatProcessor(memory_manager=MagicMock(), personal_docs_manager=MagicMock())
    session = SimpleNamespace(
        endpoint_url="http://local", model="test", headers={},
        history=[
            {"role": "user", "content": "tell me about the Acme widget"},
            {"role": "assistant", "content": "The Acme widget is a gadget."},
            {"role": "user", "content": "UNTRUSTED SOURCE DATA\nnot a human turn",
             "metadata": {"trusted": False}},
            {"role": "user", "content": "what about the price?"},
        ],
    )

    processor.build_context_preface(
        message="what about the price?", session=session,
        use_web=True, use_rag=False, use_memory=False, use_skills=False,
    )

    messages = mock_llm_call.call_args[0][2]
    assert "Today's date is" in messages[0]["content"]
    user = messages[1]["content"]
    assert "user: tell me about the Acme widget" in user
    assert "assistant: The Acme widget is a gadget." in user
    assert "not a human turn" not in user
    assert user.endswith("Latest message:\nwhat about the price?")
    assert user.count("what about the price?") == 1  # the current turn is not repeated as history
    assert mock_llm_call.call_args[1]["max_tokens"] >= 256
    mock_web_search.assert_called_with("acme widget price 2026", time_filter=None, return_sources=True)

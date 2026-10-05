"""Document retrieval at app load (static/app.js and static/js/chat.js).

The composer only tells the server to skip retrieval when the toggle is
switched off, so a toggle that starts off in a fresh browser silently turned
retrieval off for every first message.
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.browser


def send(page, text="What changed in the orders migration?") -> str:
    page.click("#message")
    page.keyboard.type(text)
    with page.expect_request(lambda r: r.method == "POST" and r.url.endswith("/api/chat_stream")) as sent:
        page.click(".send-btn")
    return sent.value.post_data or ""


def test_a_fresh_browser_starts_with_document_retrieval_on_and_asks_for_it(open_app):
    page = open_app(1440)
    assert page.evaluate("document.getElementById('rag-toggle').checked") is True
    assert 'name="use_rag"' not in send(page)


def test_a_retrieval_choice_the_user_made_is_kept(open_app):
    page = open_app(1440)
    page.evaluate("localStorage.setItem('odysseus-toggles', %s)" % json.dumps(json.dumps({"rag": False})))
    page.reload()
    page.wait_for_function("window.agentsDashboard && window.workbenchModule && document.querySelectorAll('#chat-history .msg').length >= 4")
    assert page.evaluate("document.getElementById('rag-toggle').checked") is False
    body = send(page)
    assert 'name="use_rag"' in body and "false" in body.split('name="use_rag"', 1)[1][:40]

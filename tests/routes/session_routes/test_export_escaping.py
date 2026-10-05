"""An HTML export of a chat cannot run markup from the chat's name or messages.

Chat names are user- and model-written, and the export opens in a browser.
"""
import pytest

from core.models import ChatMessage

pytestmark = pytest.mark.security

PAYLOAD = '<img src=x onerror="alert(1)">'


def test_the_html_export_escapes_the_name_and_the_messages(api):
    alice = api.as_user("alice")
    created = alice.post("/api/session", data={"name": PAYLOAD, "skip_validation": "true"})
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]
    api.session_manager.get_session(session_id).add_message(ChatMessage("assistant", PAYLOAD))

    response = alice.get(f"/api/session/{session_id}/export", params={"fmt": "html"})

    assert response.status_code == 200
    assert "<img" not in response.text
    assert "&lt;img src=x" in response.text

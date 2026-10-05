"""Opening a message in the email library (static/js/emailLibrary.js).

A message body is whatever its sender wrote. The page must not run it:
scripts, event handlers and script URLs are dropped before the body is shown,
including inside a quoted earlier reply.
"""
from __future__ import annotations

import pytest

from tests.helpers.static_app import expect, settle

pytestmark = pytest.mark.browser

BODY_HTML = (
    "<p onclick=\"window.__xss=1\">Latest reply <b>from Alex</b></p>"
    "<script>window.__xss = 2</script>"
    "<p><a href=\"javascript:window.__xss=3\">open the plan</a></p>"
    "<blockquote><p onmouseover=\"window.__xss=4\">Earlier text</p>"
    "<a href=\"javascript:window.__xss=5\">older link</a></blockquote>"
)
MESSAGE = {"uid": "101", "subject": "Quarterly plan", "from_name": "Alex", "from_address": "alex@example.com",
           "to": "me@example.com", "date": "2026-10-01T10:00:00Z", "folder": "INBOX", "seen": False}


def serve_mailbox(page):
    page.route("**/api/email/accounts*", lambda route: route.fulfill(json={"accounts": [
        {"id": "acct-1", "email": "me@example.com", "name": "Me", "is_default": True}]}))
    page.route("**/api/email/folders*", lambda route: route.fulfill(json={"folders": ["INBOX"], "sync": {}}))
    page.route("**/api/email/list*", lambda route: route.fulfill(json={"emails": [MESSAGE], "total": 1, "sync": {}}))
    page.route("**/api/email/read/*", lambda route: route.fulfill(json=dict(
        MESSAGE, body="Latest reply from Alex", body_html=BODY_HTML)))


def test_a_message_opens_without_the_scripts_and_handlers_its_sender_wrote(open_app):
    page = open_app(1440, chat=False)
    serve_mailbox(page)
    page.evaluate("document.getElementById('rail-email').click()")
    page.locator(".email-lib-row, .email-row, [data-uid='101']").first.click()
    body = page.locator(".email-reader-body")
    expect(body).to_contain_text("Latest reply")
    expect(body).to_contain_text("Earlier text")
    settle(page)
    # Clicking is where a handler or a script URL would run.
    body.locator("p").first.click()
    body.locator("a").first.click()
    assert page.evaluate("window.__xss") is None
    html = body.inner_html()
    assert "<script" not in html
    for forbidden in ("onclick", "onmouseover", "javascript:"):
        assert forbidden not in html, forbidden

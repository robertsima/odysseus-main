"""The diagnostics bundle export in Settings > System (static/js/admin.js).

2026-10-20: the server built every bundle (200, about 300 KB, 3 s), but the
zip never reached the person's Downloads. The page fetched it and started
the download from script once the build finished, after the click, and the
browser can drop a download started that way. Export must hand the request
to the browser as a download, and a failed build must say why on the page.
"""
from __future__ import annotations

import time

import pytest

from tests.helpers.static_app import expect

pytestmark = pytest.mark.browser

ZIP = b"PK\x05\x06" + b"\x00" * 18


def _open_card(open_app):
    page = open_app(1440, chat=False)
    page.click("#user-bar-settings")
    page.click('[data-settings-tab="system"]')
    page.locator("#diag-bundle-export-btn").scroll_into_view_if_needed()
    return page


def test_export_saves_the_bundle_the_server_builds(open_app):
    page = _open_card(open_app)
    requested = []

    def bundle(route):
        requested.append(route.request.url)
        time.sleep(1)  # the server takes seconds to build the zip
        route.fulfill(status=200, body=ZIP, headers={
            "Content-Type": "application/zip",
            "Content-Disposition": 'attachment; filename="odysseus-diagnostics-test.zip"',
            # What the server's security middleware adds to every API response.
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none'",
        })

    page.route("**/api/diagnostics/bundle?*", bundle)
    with page.expect_download(timeout=15000) as info:
        page.click("#diag-bundle-export-btn")
    download = info.value

    assert download.suggested_filename == "odysseus-diagnostics-test.zip"
    assert "/api/diagnostics/bundle?minutes=60" in download.url, download.url
    assert open(download.path(), "rb").read() == ZIP
    assert len(requested) == 1
    expect(page.locator("#diag-bundle-msg")).not_to_contain_text("failed")


def test_a_failed_build_says_why_and_leaves_the_page(open_app):
    page = _open_card(open_app)
    # The real server sends these on every API response, so the error page
    # in the download frame is blocked and unreadable.
    page.route("**/api/diagnostics/bundle?*", lambda route: route.fulfill(
        status=403, json={"detail": "Admin access required"},
        headers={"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"}))
    page.route("**/api/diagnostics/bundle/summary?*", lambda route: route.fulfill(
        status=403, json={"detail": "Admin access required"}))
    page.click("#diag-bundle-export-btn")

    expect(page.locator("#diag-bundle-msg")).to_contain_text("Export failed: Admin access required")
    expect(page.locator("#diag-bundle-export-btn")).to_be_visible()

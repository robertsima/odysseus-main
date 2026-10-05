"""The MCP OAuth paste-back page cannot be made to run script.

Its server id comes from the OAuth ``state`` parameter, which anyone can put
in a link, and the page is served from the app's own origin.
"""
import pytest

from routes.mcp.mcp_routes import _oauth_authorize_page

pytestmark = pytest.mark.security

ATTACK = '"><script>alert(1)</script>'


def test_reflected_values_are_escaped():
    page = _oauth_authorize_page(
        "https://accounts.google.com/o/oauth2/v2/auth?state=" + ATTACK,
        "gmail" + ATTACK,
        "https://odysseus.example/api/mcp/oauth/callback" + ATTACK,
    )

    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;alert(1)" in page

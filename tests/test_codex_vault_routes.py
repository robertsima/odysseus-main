"""An external agent session can read the same vault Odysseus retrieves from.

This is the sanctioned direction of the Claude Code integration: Claude Code
is the client and Odysseus is the data server, so no credential is ever
intermediated. The vault was the one context store the scoped `/api/codex/*`
API did not expose — todos, memory, calendar, email and the editor library
were reachable, the Markdown notes were not.

Privacy is the load-bearing detail: an agent session ships retrieved text to
a hosted provider, so private-labelled directories (typically `Journal`) need
a second, explicit scope. The chat path gates the same content on
`is_local_endpoint` for the same reason.
"""
import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.area_security


def _token_request(scopes, owner="alice"):
    from types import SimpleNamespace

    return SimpleNamespace(
        state=SimpleNamespace(
            api_token=True, api_token_scopes=list(scopes), api_token_owner=owner, current_user="api",
        ),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
        headers={},
    )


def test_the_vault_routes_are_registered():
    from routes.codex_routes import setup_codex_routes

    routes = {(m, r.path) for r in setup_codex_routes().routes for m in r.methods}
    assert ("GET", "/api/codex/vault/search") in routes
    assert ("GET", "/api/codex/vault/document") in routes


def test_read_scope_is_required_and_private_is_a_separate_scope():
    from routes.codex_routes import VAULT_PRIVATE_SCOPES, VAULT_READ_SCOPES, _scope_owner

    assert _scope_owner(_token_request(["vault:read"]), VAULT_READ_SCOPES) == "alice"
    assert _scope_owner(_token_request(["vault:read_private"]), VAULT_READ_SCOPES) == "alice"
    with pytest.raises(HTTPException) as exc:
        _scope_owner(_token_request(["todos:read"]), VAULT_READ_SCOPES)
    assert exc.value.status_code == 403
    # A plain read scope does not reach private notes.
    with pytest.raises(HTTPException):
        _scope_owner(_token_request(["vault:read"]), VAULT_PRIVATE_SCOPES)


def test_the_scopes_and_the_agent_profile_are_registered():
    from routes.api_token_routes import ALLOWED_SCOPES, TOKEN_PROFILES, _normalize_scopes

    assert {"vault:read", "vault:read_private"} <= ALLOWED_SCOPES
    # The default agent token gets the shared context store, public only.
    assert "vault:read" in TOKEN_PROFILES["claude_agent"]
    assert "vault:read_private" not in TOKEN_PROFILES["claude_agent"]
    normalized = _normalize_scopes(["vault:read_private"])
    assert normalized.index("vault:read") < normalized.index("vault:read_private")

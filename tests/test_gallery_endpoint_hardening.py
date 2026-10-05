"""Focused security tests for gallery endpoint URL hardening.

Covers:
- _is_openai_api_base: exact hostname matching (no substring bypass)
- _join_checked_gallery_endpoint: allowlist-only path construction
"""
import routes.gallery_routes as gallery_routes


# ---------------------------------------------------------------------------
# _is_openai_api_base — exact hostname, no substring tricks
# ---------------------------------------------------------------------------

def test_is_openai_api_base_accepts_exact_host():
    f = gallery_routes._is_openai_api_base
    assert f("https://api.openai.com") is True
    assert f("https://api.openai.com/v1") is True
    assert f("https://api.openai.com/") is True
    assert f("api.openai.com") is True


def test_is_openai_api_base_rejects_path_embed():
    # attacker hides api.openai.com in the path, not the hostname
    f = gallery_routes._is_openai_api_base
    assert f("https://evil.test/api.openai.com/v1") is False


def test_is_openai_api_base_rejects_subdomain_suffix():
    # hostname ends with .openai.com but isn't exactly api.openai.com
    f = gallery_routes._is_openai_api_base
    assert f("https://api.openai.com.evil.test/v1") is False
    assert f("https://evil-api.openai.com/v1") is False
    assert f("https://notapi.openai.com/v1") is False


def test_is_openai_api_base_rejects_malformed():
    f = gallery_routes._is_openai_api_base
    assert f("") is False
    assert f("not a url at all !!!") is False


# ---------------------------------------------------------------------------
# _join_checked_gallery_endpoint — allowlist enforcement
# ---------------------------------------------------------------------------

def test_join_checked_accepts_known_paths():
    j = gallery_routes._join_checked_gallery_endpoint
    assert j("http://localhost:7860/v1", "/images/img2img") == "http://localhost:7860/v1/images/img2img"
    assert j("http://localhost:7860", "/sdapi/v1/img2img") == "http://localhost:7860/sdapi/v1/img2img"
    assert j("https://api.openai.com/v1", "/images/edits") == "https://api.openai.com/v1/images/edits"


def test_join_checked_rejects_unknown_path():
    import pytest
    j = gallery_routes._join_checked_gallery_endpoint
    with pytest.raises(ValueError):
        j("http://localhost/v1", "/arbitrary/user/path")
    with pytest.raises(ValueError):
        j("http://localhost/v1", "")
    with pytest.raises(ValueError):
        j("http://localhost/v1", "https://evil.test/steal")


# ---------------------------------------------------------------------------
# _is_openai_api_base — userinfo bypass
# ---------------------------------------------------------------------------

def test_is_openai_api_base_rejects_userinfo_bypass():
    # userinfo trick: user = api.openai.com, host = evil.test
    f = gallery_routes._is_openai_api_base
    assert f("https://api.openai.com@evil.test/v1") is False

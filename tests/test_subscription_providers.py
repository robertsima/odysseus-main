"""Subscription auth must be provider-neutral, and must stay honest when it isn't.

Two things are being protected here.

The first is the billing model. Odysseus reaches Claude by shelling out to a CLI
because that is the only path that spends a *subscription* instead of metered
per-token API credit; the generalisation exists so a Claude subscription can be
connected like a ChatGPT one, not so an API key can be swapped in. So these tests
assert the ChatGPT adapter still routes through the module that already works,
unchanged.

The second is honesty about what is not known. The Claude provider ships with
unverified OAuth constants left blank on purpose, and the failure mode to avoid is
a provider that looks connectable and isn't. ``is_linked()`` must therefore say
"not configured" in plain words rather than raising, and the provider must claim
no URLs while it is in that state.

Nothing here touches the network: every HTTP-shaped call is either mocked at the
delegation boundary or blocked by the unconfigured guard.
"""

from __future__ import annotations

import pytest

from src.subscription import base


# --- the interface --------------------------------------------------------


def test_registry_lists_only_compliant_providers():
    import src.subscription as subscription

    ids = [p.provider_id for p in subscription.providers()]
    assert "chatgpt-subscription" in ids
    # Claude.ai consumer OAuth is intentionally not an Odysseus provider. Its
    # subscription must be used through the unmodified Claude Code/MCP path.
    assert "claude-subscription" not in ids


def test_get_returns_provider_and_none_for_unknown():
    import src.subscription as subscription

    assert subscription.get("chatgpt-subscription") is not None
    # A row written by a build with a provider this one lacks is normal, not an error.
    assert subscription.get("totally-unknown") is None


@pytest.mark.parametrize("provider_id", ["chatgpt-subscription"])
def test_providers_satisfy_the_contract(provider_id):
    """Registered providers implement the whole interface, not a subset."""
    import src.subscription as subscription

    provider = subscription.get(provider_id)
    assert isinstance(provider, base.SubscriptionProvider)
    for name in (
        "owns_base_url",
        "request_device_code",
        "poll_device_auth",
        "exchange_authorization_code",
        "refresh_oauth_tokens",
        "access_token_is_expiring",
        "resolve_runtime_credentials",
        "headers",
        "is_linked",
    ):
        assert callable(getattr(provider, name)), f"{provider_id} is missing {name}"
    assert provider.title
    assert provider.auth_mode


def test_providers_expose_no_api_key_path():
    """An API key is a different billing model, so no provider may offer one.

    Guards the constraint this whole package exists for: a helpful-looking
    ``api_key`` parameter or setter is how a subscription quietly becomes a
    metered account.
    """
    import src.subscription as subscription

    for provider in subscription.providers():
        names = [n for n in dir(provider) if not n.startswith("_")]
        offenders = [n for n in names if "api_key" in n or "apikey" in n.lower()]
        assert not offenders, f"{provider.provider_id} exposes {offenders}"


# --- ChatGPT adapter delegates -------------------------------------------


def test_chatgpt_adapter_delegates_every_step(monkeypatch):
    """The adapter must call the existing module, not reimplement it."""
    from src.subscription import chatgpt as adapter

    calls = []

    def _record(name, result):
        def _fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result
        return _fn

    monkeypatch.setattr(adapter._impl, "request_device_code",
                        _record("device_code", {"device_auth_id": "d", "user_code": "u"}))
    monkeypatch.setattr(adapter._impl, "poll_device_auth",
                        _record("poll", {"status": "pending"}))
    monkeypatch.setattr(adapter._impl, "exchange_authorization_code",
                        _record("exchange", {"access_token": "at"}))
    monkeypatch.setattr(adapter._impl, "refresh_oauth_tokens",
                        _record("refresh", {"access_token": "at2"}))
    monkeypatch.setattr(adapter._impl, "resolve_runtime_credentials",
                        _record("resolve", {"api_key": "at3"}))
    monkeypatch.setattr(adapter._impl, "chatgpt_headers",
                        _record("headers", {"Authorization": "Bearer at"}))
    monkeypatch.setattr(adapter._impl, "is_chatgpt_subscription_base", _record("owns", True))
    monkeypatch.setattr(adapter._impl, "access_token_is_expiring", _record("expiring", False))

    provider = adapter.provider()
    assert provider.request_device_code() == {"device_auth_id": "d", "user_code": "u"}
    assert provider.poll_device_auth("d", "u") == {"status": "pending"}
    assert provider.exchange_authorization_code("code", "verifier") == {"access_token": "at"}
    assert provider.refresh_oauth_tokens("old", "refresh") == {"access_token": "at2"}
    assert provider.resolve_runtime_credentials("auth-1", "owner") == {"api_key": "at3"}
    assert provider.headers("at") == {"Authorization": "Bearer at"}
    assert provider.owns_base_url("https://chatgpt.com/backend-api/codex") is True
    assert provider.access_token_is_expiring("tok") is False

    assert [name for name, _a, _k in calls] == [
        "device_code", "poll", "exchange", "refresh", "resolve",
        "headers", "owns", "expiring",
    ]


def test_chatgpt_adapter_forwards_resolve_arguments(monkeypatch):
    """owner and force_refresh must reach the underlying resolve untouched.

    owner is the tenancy boundary and force_refresh is how a caller recovers from
    a rejected token; dropping either looks like it works until it doesn't.
    """
    from src.subscription import chatgpt as adapter

    seen = {}

    def _resolve(auth_id, owner=None, *, force_refresh=False):
        seen.update(auth_id=auth_id, owner=owner, force_refresh=force_refresh)
        return {"api_key": "tok"}

    monkeypatch.setattr(adapter._impl, "resolve_runtime_credentials", _resolve)
    adapter.provider().resolve_runtime_credentials("a1", "robert", force_refresh=True)
    assert seen == {"auth_id": "a1", "owner": "robert", "force_refresh": True}


def test_chatgpt_adapter_reads_base_url_through_the_module(monkeypatch):
    """The base URL is env-driven, so the adapter must not snapshot it."""
    from src.subscription import chatgpt as adapter

    monkeypatch.setattr(
        adapter._impl, "DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL", "https://example.invalid/codex"
    )
    assert adapter.provider().default_base_url == "https://example.invalid/codex"


def test_chatgpt_adapter_keeps_provider_id_of_the_existing_rows():
    """Stored credentials are keyed by this string; it cannot drift."""
    from src.subscription import chatgpt as adapter
    from src import chatgpt_subscription

    assert adapter.PROVIDER_ID == chatgpt_subscription.CHATGPT_SUBSCRIPTION_PROVIDER
    assert adapter.provider().auth_mode == "chatgpt"


# --- error taxonomy maps both ways ---------------------------------------


@pytest.mark.parametrize(
    "native_name, neutral_cls",
    [
        ("ChatGPTSubscriptionRateLimited", base.SubscriptionRateLimited),
        ("ChatGPTSubscriptionReauthRequired", base.SubscriptionReauthRequired),
        ("ChatGPTSubscriptionAuthNotFound", base.SubscriptionAuthNotFound),
        ("ChatGPTSubscriptionError", base.SubscriptionError),
    ],
)
def test_native_errors_map_to_neutral(native_name, neutral_cls):
    from src import chatgpt_subscription
    from src.subscription import chatgpt as adapter

    native = getattr(chatgpt_subscription, native_name)("upstream said no")
    mapped = adapter.to_neutral_error(native)
    assert type(mapped) is neutral_cls
    assert str(mapped) == "upstream said no"          # message survives verbatim
    assert mapped.provider == adapter.PROVIDER_ID      # caller can say which account
    assert mapped.__cause__ is native                  # traceback still reaches the origin


@pytest.mark.parametrize(
    "neutral_cls, native_name",
    [
        (base.SubscriptionRateLimited, "ChatGPTSubscriptionRateLimited"),
        (base.SubscriptionReauthRequired, "ChatGPTSubscriptionReauthRequired"),
        (base.SubscriptionAuthNotFound, "ChatGPTSubscriptionAuthNotFound"),
        (base.SubscriptionError, "ChatGPTSubscriptionError"),
    ],
)
def test_neutral_errors_map_back_to_native(neutral_cls, native_name):
    from src import chatgpt_subscription
    from src.subscription import chatgpt as adapter

    mapped = adapter.to_native_error(neutral_cls("quota gone"))
    assert type(mapped) is getattr(chatgpt_subscription, native_name)
    assert str(mapped) == "quota gone"


def test_adapter_raises_neutral_errors_from_delegated_calls(monkeypatch):
    """A caller that only knows the interface must still be able to catch failures."""
    from src import chatgpt_subscription
    from src.subscription import chatgpt as adapter

    def _boom(*_args, **_kwargs):
        raise chatgpt_subscription.ChatGPTSubscriptionReauthRequired("token is dead")

    monkeypatch.setattr(adapter._impl, "resolve_runtime_credentials", _boom)
    with pytest.raises(base.SubscriptionReauthRequired) as excinfo:
        adapter.provider().resolve_runtime_credentials("a1")
    assert "token is dead" in str(excinfo.value)


def test_http_statuses_match_the_existing_module():
    """The admin UI keys its messaging off these, so they must not shift."""
    from src.subscription import chatgpt as adapter

    assert adapter.to_http_exception(base.SubscriptionRateLimited("slow down")).status_code == 429
    assert adapter.to_http_exception(base.SubscriptionReauthRequired("dead")).status_code == 401
    assert adapter.to_http_exception(base.SubscriptionAuthNotFound("none")).status_code == 401
    assert adapter.to_http_exception(base.SubscriptionError("upstream")).status_code == 502
    # Unconfigured is not the user's fault and no credential fixes it.
    assert base.to_http_exception(base.SubscriptionNotConfigured("no endpoints")).status_code == 503


def test_native_errors_keep_their_existing_http_mapping():
    from src import chatgpt_subscription
    from src.subscription import chatgpt as adapter

    native = chatgpt_subscription.ChatGPTSubscriptionRateLimited("quota")
    assert adapter.to_http_exception(native).status_code == 429
    assert (
        adapter.to_http_exception(native).detail
        == chatgpt_subscription.to_http_exception(native).detail
    )


# --- routing --------------------------------------------------------------


def test_provider_for_url_routes_chatgpt():
    import src.subscription as subscription

    provider = subscription.provider_for_url("https://chatgpt.com/backend-api/codex")
    assert provider is not None
    assert provider.provider_id == "chatgpt-subscription"
    # Sub-paths belong to the same subscription.
    assert (
        subscription.provider_for_url(
            "https://chatgpt.com/backend-api/codex/responses"
        ).provider_id
        == "chatgpt-subscription"
    )


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://api.openai.com/v1",
        "https://api.anthropic.com/v1",   # an API-key endpoint: no subscription owns it
        "http://localhost:11434/v1",
        "https://chatgpt.com/",           # right host, not the subscription backend
    ],
)
def test_provider_for_url_returns_none_for_unrelated_urls(url):
    import src.subscription as subscription

    assert subscription.provider_for_url(url) is None


def test_unconfigured_claude_claims_no_urls(monkeypatch):
    """An unconfigured provider must not capture a URL it cannot serve."""
    from src.subscription import claude

    monkeypatch.setattr(claude, "DEFAULT_CLAUDE_SUBSCRIPTION_BASE_URL", "")
    provider = claude.provider()
    assert provider.owns_base_url("https://api.anthropic.com/v1") is False
    assert provider.owns_base_url("") is False


def test_configured_claude_matches_only_its_own_base(monkeypatch):
    from src.subscription import claude

    monkeypatch.setattr(
        claude, "DEFAULT_CLAUDE_SUBSCRIPTION_BASE_URL", "https://example.invalid/sub/v1"
    )
    provider = claude.provider()
    # The module is retained only as a fail-closed compatibility placeholder;
    # environment values must not re-enable consumer OAuth.
    assert provider.owns_base_url("https://example.invalid/sub/v1") is False
    assert provider.owns_base_url("https://example.invalid/sub/v1/messages") is False
    assert provider.owns_base_url("https://example.invalid/other") is False
    assert provider.owns_base_url("https://elsewhere.invalid/sub/v1") is False


# --- the unconfigured Claude provider is honest about it ------------------


def test_claude_is_linked_reports_not_configured_without_raising(monkeypatch):
    from src.subscription import claude

    monkeypatch.setattr(claude, "CLAUDE_OAUTH_CLIENT_ID", "")
    monkeypatch.setattr(claude, "CLAUDE_OAUTH_TOKEN_URL", "")
    monkeypatch.setattr(claude, "CLAUDE_OAUTH_REDIRECT_URI", "")

    linked, reason = claude.is_linked()
    assert linked is False
    assert "disabled" in reason.lower()


def test_claude_is_linked_matches_the_capability_registry_contract():
    """src.capabilities_builtin calls the module-level function and expects a pair."""
    from src.subscription import claude

    result = claude.is_linked()
    assert isinstance(result, tuple) and len(result) == 2
    assert isinstance(result[0], bool) and isinstance(result[1], str)


def test_claude_login_steps_refuse_rather_than_call_a_guessed_endpoint(monkeypatch):
    """Unverified endpoints must fail closed, never reach the network."""
    from src.subscription import claude

    monkeypatch.setattr(claude, "CLAUDE_OAUTH_CLIENT_ID", "")
    monkeypatch.setattr(claude, "CLAUDE_OAUTH_TOKEN_URL", "")
    monkeypatch.setattr(claude, "CLAUDE_OAUTH_REDIRECT_URI", "")
    monkeypatch.setattr(claude, "CLAUDE_OAUTH_DEVICE_CODE_URL", "")
    monkeypatch.setattr(claude, "CLAUDE_OAUTH_DEVICE_TOKEN_URL", "")

    def _no_network(*_args, **_kwargs):
        raise AssertionError("an unconfigured provider must not make a request")

    monkeypatch.setattr(claude.httpx, "post", _no_network)

    provider = claude.provider()
    assert provider.supports_device_flow is False
    for call in (
        lambda: provider.request_device_code(),
        lambda: provider.poll_device_auth("d", "u"),
        lambda: provider.exchange_authorization_code("code", "verifier"),
        lambda: provider.refresh_oauth_tokens("old", "refresh"),
    ):
        with pytest.raises(base.SubscriptionNotConfigured):
            call()


def test_claude_refresh_without_a_refresh_token_asks_for_reconnect(monkeypatch):
    """Configured but credential-less: reconnect, which is a different fix."""
    from src.subscription import claude

    monkeypatch.setattr(claude, "CLAUDE_OAUTH_CLIENT_ID", "client-under-test")
    monkeypatch.setattr(claude, "CLAUDE_OAUTH_TOKEN_URL", "https://example.invalid/token")
    monkeypatch.setattr(claude, "CLAUDE_OAUTH_REDIRECT_URI", "https://example.invalid/cb")

    def _no_network(*_args, **_kwargs):
        raise AssertionError("must not post without a refresh token")

    monkeypatch.setattr(claude.httpx, "post", _no_network)
    with pytest.raises(base.SubscriptionNotConfigured):
        claude.provider().refresh_oauth_tokens("old", "")


def test_claude_headers_use_a_bearer_not_an_api_key_header():
    """An OAuth subscription token in x-api-key is a billing-model mistake."""
    from src.subscription import claude

    headers = claude.provider().headers("subscription-token")
    assert headers["Authorization"] == "Bearer subscription-token"
    assert "x-api-key" not in {k.lower() for k in headers}
    assert headers["anthropic-version"]
    # No token, no Authorization header at all — not an empty bearer.
    assert "Authorization" not in claude.provider().headers(None)


def test_claude_pkce_pair_is_a_valid_s256_challenge():
    import base64 as _b64
    import hashlib

    from src.subscription import claude

    verifier, challenge = claude.generate_pkce_pair()
    expected = (
        _b64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert challenge == expected
    assert "=" not in verifier and 43 <= len(verifier) <= 128
    # Fresh randomness per login, or one intercepted code compromises the next.
    assert claude.generate_pkce_pair()[0] != verifier


# --- refresh skew ---------------------------------------------------------


def _jwt_expiring_in(seconds: int) -> str:
    """A token with only the claim the skew logic reads. Not a real credential."""
    import base64 as _b64
    import json
    import time

    payload = _b64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time()) + seconds}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


@pytest.mark.parametrize(
    "expires_in, skew, expected",
    [
        (3600, 120, False),   # comfortably valid
        (60, 120, True),      # inside the skew window: refresh before using it
        (121, 120, False),    # just outside
        (120, 120, True),     # boundary counts as expiring
        (-5, 120, True),      # already dead
    ],
)
def test_skew_window_decides_when_to_refresh(expires_in, skew, expected):
    assert base.jwt_access_token_is_expiring(_jwt_expiring_in(expires_in), skew) is expected


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b", "a.!!!.c"])
def test_unreadable_tokens_count_as_expiring(token):
    """A token we cannot inspect is one we cannot trust; refreshing is the cheap side."""
    assert base.jwt_access_token_is_expiring(token) is True


def test_provider_skew_default_is_honoured(monkeypatch):
    """Each provider's own skew applies when the caller does not pass one."""
    from src.subscription import claude

    provider = claude.provider()
    monkeypatch.setattr(type(provider), "refresh_skew_seconds", 600)
    assert provider.access_token_is_expiring(_jwt_expiring_in(300)) is True
    assert provider.access_token_is_expiring(_jwt_expiring_in(300), 60) is False


def test_chatgpt_skew_default_comes_from_the_existing_module():
    from src import chatgpt_subscription
    from src.subscription import chatgpt as adapter

    assert (
        adapter.provider().refresh_skew_seconds
        == chatgpt_subscription.CHATGPT_ACCESS_TOKEN_REFRESH_SKEW_SECONDS
    )


def test_chatgpt_adapter_passes_its_skew_to_the_existing_check(monkeypatch):
    from src.subscription import chatgpt as adapter

    seen = {}

    def _expiring(token, skew):
        seen.update(token=token, skew=skew)
        return False

    monkeypatch.setattr(adapter._impl, "access_token_is_expiring", _expiring)
    provider = adapter.provider()
    provider.access_token_is_expiring("tok")
    assert seen["skew"] == provider.refresh_skew_seconds
    provider.access_token_is_expiring("tok", 5)
    assert seen["skew"] == 5


# --- the shared refresh-on-read resolve ----------------------------------


class _Row:
    """The columns of a ProviderAuthSession row the resolve path touches."""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", "auth-1")
        self.provider = kwargs.get("provider", "claude-subscription")
        self.owner = kwargs.get("owner")
        self.base_url = kwargs.get("base_url", "")
        self.access_token = kwargs.get("access_token", "")
        self.refresh_token = kwargs.get("refresh_token", "")
        self.last_refresh = None
        self.auth_mode = kwargs.get("auth_mode")


class _Query:
    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row


class _Session:
    def __init__(self, row):
        self._row = row
        self.committed = 0

    def query(self, *_args, **_kwargs):
        return _Query(self._row)

    def refresh(self, _row):
        return None

    def commit(self):
        self.committed += 1

    def close(self):
        return None


class _Model:
    """Stands in for the ProviderAuthSession mapped class in filter expressions."""

    id = "id"
    provider = "provider"
    owner = "owner"


def _patch_db(monkeypatch, row):
    session = _Session(row)
    monkeypatch.setattr(
        base, "database_handles", lambda: (_Model, lambda: session, lambda: "now")
    )
    return session


def test_resolve_refreshes_a_spent_token_and_persists_the_rotation(monkeypatch):
    row = _Row(access_token=_jwt_expiring_in(10), refresh_token="rt-old",
               base_url="https://example.invalid/sub")
    session = _patch_db(monkeypatch, row)

    fresh = _jwt_expiring_in(3600)
    creds = base.resolve_runtime_credentials_via_db(
        "auth-1", None,
        provider_id="claude-subscription",
        default_base_url="https://fallback.invalid",
        auth_mode="claude",
        refresh=lambda _at, _rt: {"access_token": fresh, "refresh_token": "rt-new"},
        is_expiring=lambda token: base.jwt_access_token_is_expiring(token, 120),
    )
    assert creds["api_key"] == fresh
    assert creds["base_url"] == "https://example.invalid/sub"
    assert creds["provider"] == "claude-subscription"
    assert row.refresh_token == "rt-new"
    assert session.committed == 1


def test_resolve_keeps_the_old_refresh_token_when_none_is_returned(monkeypatch):
    """Providers that do not rotate omit the field; blanking it breaks the next refresh."""
    row = _Row(access_token=_jwt_expiring_in(10), refresh_token="rt-keep")
    _patch_db(monkeypatch, row)

    base.resolve_runtime_credentials_via_db(
        "auth-1", None,
        provider_id="claude-subscription",
        default_base_url="https://fallback.invalid",
        auth_mode="claude",
        refresh=lambda _at, _rt: {"access_token": _jwt_expiring_in(3600)},
        is_expiring=lambda token: base.jwt_access_token_is_expiring(token, 120),
    )
    assert row.refresh_token == "rt-keep"


def test_resolve_does_not_refresh_a_healthy_token(monkeypatch):
    row = _Row(access_token=_jwt_expiring_in(3600), refresh_token="rt")
    session = _patch_db(monkeypatch, row)

    def _must_not_refresh(*_args, **_kwargs):
        raise AssertionError("a valid token must not be spent on a refresh")

    creds = base.resolve_runtime_credentials_via_db(
        "auth-1", None,
        provider_id="claude-subscription",
        default_base_url="https://fallback.invalid",
        auth_mode="claude",
        refresh=_must_not_refresh,
        is_expiring=lambda token: base.jwt_access_token_is_expiring(token, 120),
    )
    assert creds["api_key"] == row.access_token
    assert session.committed == 0


def test_resolve_force_refresh_overrides_a_healthy_token(monkeypatch):
    """How a caller recovers when the provider rejected a token we thought was fine."""
    row = _Row(access_token=_jwt_expiring_in(3600), refresh_token="rt")
    _patch_db(monkeypatch, row)

    refreshed = _jwt_expiring_in(7200)
    creds = base.resolve_runtime_credentials_via_db(
        "auth-1", None,
        provider_id="claude-subscription",
        default_base_url="https://fallback.invalid",
        auth_mode="claude",
        refresh=lambda _at, _rt: {"access_token": refreshed},
        is_expiring=lambda token: base.jwt_access_token_is_expiring(token, 120),
        force_refresh=True,
    )
    assert creds["api_key"] == refreshed


def test_resolve_falls_back_to_the_provider_default_base_url(monkeypatch):
    row = _Row(access_token=_jwt_expiring_in(3600), base_url="")
    _patch_db(monkeypatch, row)

    creds = base.resolve_runtime_credentials_via_db(
        "auth-1", None,
        provider_id="claude-subscription",
        default_base_url="https://fallback.invalid/v1/",
        auth_mode="claude",
        refresh=lambda _at, _rt: {"access_token": "unused"},
        is_expiring=lambda _token: False,
    )
    assert creds["base_url"] == "https://fallback.invalid/v1"   # trailing slash trimmed


def test_resolve_raises_auth_not_found_for_a_missing_row(monkeypatch):
    _patch_db(monkeypatch, None)

    with pytest.raises(base.SubscriptionAuthNotFound):
        base.resolve_runtime_credentials_via_db(
            "missing", "robert",
            provider_id="claude-subscription",
            default_base_url="https://fallback.invalid",
            auth_mode="claude",
            refresh=lambda _at, _rt: {"access_token": "x"},
            is_expiring=lambda _token: False,
        )


def test_refresh_locks_are_per_credential():
    """Serialising unrelated accounts against one lock would be a needless stall."""
    assert base.refresh_lock_for("auth-a") is base.refresh_lock_for("auth-a")
    assert base.refresh_lock_for("auth-a") is not base.refresh_lock_for("auth-b")


def test_stored_auth_row_check_never_raises(monkeypatch):
    """It feeds capability probes on request paths: a broken DB means "not linked"."""
    def _boom():
        raise RuntimeError("database is not up")

    monkeypatch.setattr(base, "database_handles", _boom)
    assert base.stored_auth_row_exists("claude-subscription") is False

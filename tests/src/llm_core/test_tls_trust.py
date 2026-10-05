"""Model calls trust the operator's extra CA bundle (LLM_CA_BUNDLE, issue #722).

A provider behind a private root CA (GigaChat, a corporate gateway) fails
every call with CERTIFICATE_VERIFY_FAILED unless the shared model client is
built with the extended trust context.
"""
import ssl

import httpx

from src import llm_core, tls_overrides


def test_the_shared_model_client_uses_the_extended_trust_context(monkeypatch):
    extended = ssl.create_default_context()
    monkeypatch.setattr(tls_overrides, "_SHARED_SSL_CONTEXT", extended)
    monkeypatch.setattr(llm_core, "_http_client", None)
    built = []

    class RecordingClient:
        is_closed = False

        def __init__(self, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RecordingClient)

    llm_core._get_http_client()

    assert built[0]["verify"] is extended

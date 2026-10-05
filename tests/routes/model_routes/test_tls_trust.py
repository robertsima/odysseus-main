"""Endpoint probes trust the operator's extra CA bundle (LLM_CA_BUNDLE, issue #722).

Without it, a provider behind a private root CA answers the model probe with
CERTIFICATE_VERIFY_FAILED and the endpoint is shown as offline.
"""
import ssl

import httpx

from routes import model_routes
from src import tls_overrides


def test_the_model_probe_uses_the_extended_trust_context(monkeypatch):
    extended = ssl.create_default_context()
    monkeypatch.setattr(tls_overrides, "_SHARED_SSL_CONTEXT", extended)
    verifies = []

    def fake_get(url, **kwargs):
        verifies.append(kwargs.get("verify"))
        return httpx.Response(200, json={"data": [{"id": "llama-3.1-8b-instruct"}]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    models = model_routes._probe_endpoint("http://127.0.0.1:9/v1")

    assert "llama-3.1-8b-instruct" in models
    assert verifies and all(v is extended for v in verifies)

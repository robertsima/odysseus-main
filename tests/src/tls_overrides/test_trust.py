"""LLM_CA_BUNDLE adds trust for a private-CA LLM provider and never removes any.

Operators point LLM_CA_BUNDLE at a PEM with an extra root (issue #722,
GigaChat). The context built from it must keep full certificate and host
checking, and building it must not change the process-wide TLS default.
"""
import datetime
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from src import tls_overrides

pytestmark = pytest.mark.security


def _ca_pem(common_name="Odysseus Test Root"):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def test_the_extra_bundle_extends_trust_and_keeps_verification_on(tmp_path, monkeypatch):
    bundle = tmp_path / "extra-roots.pem"
    bundle.write_bytes(_ca_pem())
    monkeypatch.setattr(tls_overrides, "_extra_bundle_path", str(bundle))
    default_https_context = ssl._create_default_https_context
    # Restored after the test even if the code under test replaces it.
    monkeypatch.setattr(ssl, "_create_default_https_context", default_https_context)

    ctx = tls_overrides._build_ssl_context()

    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    subjects = [dict(item[0] for item in ca["subject"]) for ca in ctx.get_ca_certs()]
    assert {"commonName": "Odysseus Test Root"} in subjects
    assert ssl._create_default_https_context is default_https_context


def test_without_a_bundle_llm_calls_use_the_default_trust_store(monkeypatch):
    monkeypatch.setattr(tls_overrides, "_extra_bundle_path", None)
    monkeypatch.setattr(tls_overrides, "_SHARED_SSL_CONTEXT", tls_overrides._build_ssl_context())

    assert tls_overrides.llm_verify() is True


def test_a_bundle_path_that_does_not_exist_keeps_the_default_trust_store(tmp_path, monkeypatch):
    monkeypatch.setattr(tls_overrides, "_extra_bundle_path", str(tmp_path / "missing.pem"))
    monkeypatch.setattr(tls_overrides, "_SHARED_SSL_CONTEXT", tls_overrides._build_ssl_context())

    assert tls_overrides.llm_verify() is True

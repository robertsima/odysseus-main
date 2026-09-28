"""A stand-in for github.com: one bare repository over smart HTTPS.

``manage_git fetch`` only talks to credential-free ``https://`` remotes on
github.com or the Enterprise host ``GITHUB_HOST`` names, on port 443
(src/agent_worktree/repository_sync.py ``_https_url``). To exercise that code
path without the network, run.sh points ``GITHUB_HOST`` at a local name
(git.e2e.test, added to /etc/hosts), gives the app a CA bundle that trusts the
certificate made here, and runs this server on 127.0.0.1:443 (needs root for
the port).

    python githost.py cert --dir DIR --host git.e2e.test
    python githost.py serve --repo /path/remote.git --slug robertsima/Umni \
        --cert DIR/server.pem --key DIR/server.key [--port 443]
"""

from __future__ import annotations

import argparse
import datetime
import os
import ssl
import sys
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server


def make_cert(directory: str, host: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    os.makedirs(directory, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Odysseus e2e test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(ca_name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    with open(os.path.join(directory, "ca.pem"), "wb") as fh:
        fh.write(ca.public_bytes(pem))
    with open(os.path.join(directory, "server.pem"), "wb") as fh:
        fh.write(cert.public_bytes(pem))
    with open(os.path.join(directory, "server.key"), "wb") as fh:
        fh.write(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


class _Quiet(WSGIRequestHandler):
    def log_message(self, fmt, *args):  # one line per request on stderr
        sys.stderr.write("[githost] " + (fmt % args) + "\n")


class _ThreadingServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def _with_content_length(app):
    """Buffer each response and give it a Content-Length.

    dulwich.web streams its responses, so wsgiref sends them close-delimited;
    over TLS the socket then closes without a close_notify and OpenSSL 3
    clients (git, curl, urllib3) report "unexpected eof" instead of a body.
    """
    def wrapper(environ, start_response):
        captured = {}
        body = []

        def capture(status, headers, exc_info=None):
            captured["status"], captured["headers"] = status, headers
            return body.append

        result = app(environ, capture)
        try:
            for chunk in result:
                body.append(chunk)
        finally:
            close = getattr(result, "close", None)
            if callable(close):
                close()
        data = b"".join(body)
        headers = [(k, v) for k, v in captured.get("headers", [])
                   if k.lower() not in ("content-length", "transfer-encoding")]
        headers.append(("Content-Length", str(len(data))))
        start_response(captured.get("status", "500 Internal Server Error"), headers)
        return [data]

    return wrapper


def serve(repo: str, slug: str, cert: str, key: str, host: str, port: int) -> None:
    from dulwich.errors import NotGitRepository
    from dulwich.repo import Repo
    from dulwich.server import Backend
    from dulwich.web import make_wsgi_chain

    wanted = {f"/{slug}", f"/{slug}.git", f"/{slug}/", f"/{slug}.git/"}

    class OneRepo(Backend):
        def open_repository(self, path):
            path = path.decode() if isinstance(path, bytes) else path
            if path in wanted or path.rstrip("/") in wanted:
                return Repo(repo)
            raise NotGitRepository(f"no repository at {path}")

    wsgi = _with_content_length(make_wsgi_chain(OneRepo()))
    server = make_server(host, port, wsgi, server_class=_ThreadingServer, handler_class=_Quiet)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    sys.stderr.write(f"[githost] serving {repo} as https://<host>:{port}/{slug}.git\n")
    sys.stderr.flush()
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cert")
    c.add_argument("--dir", required=True)
    c.add_argument("--host", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--repo", required=True)
    s.add_argument("--slug", required=True)
    s.add_argument("--cert", required=True)
    s.add_argument("--key", required=True)
    s.add_argument("--bind", default="127.0.0.1")
    s.add_argument("--port", type=int, default=443)
    args = parser.parse_args()
    if args.cmd == "cert":
        make_cert(args.dir, args.host)
    else:
        serve(args.repo, args.slug, args.cert, args.key, args.bind, args.port)


if __name__ == "__main__":
    main()

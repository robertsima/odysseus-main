"""The vault browser lists PDF, Word and image files and serves them raw.

Over HTTP against the real app, so the auth dependency and the response
headers are the ones a browser sees.
"""
import os

import pytest

import src.rag_sensitivity as sensitivity

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "Clients" / "Acme" / "Agreements").mkdir(parents=True)
    agreements = root / "Clients" / "Acme" / "Agreements"
    (agreements / "msa.docx").write_bytes(b"PK\x03\x04docx")
    (agreements / "msa.pdf").write_bytes(PDF)
    (agreements / "~$msa.docx").write_bytes(b"lock")
    (root / "notes.md").write_text("# hi", encoding="utf-8")
    (root / "logo.png").write_bytes(PNG)
    (root / ".hidden.pdf").write_bytes(PDF)
    (root / "script.exe").write_bytes(b"MZ")
    (root / "vector.svg").write_text("<svg onload=alert(1)/>", encoding="utf-8")
    monkeypatch.setattr(sensitivity, "vault_root", lambda: str(root))
    monkeypatch.setattr(sensitivity, "_safe_folder_policy_map", lambda: ({}, True))
    return root


def _files(node):
    if node["type"] == "file":
        return [node]
    return [f for child in node.get("children", []) for f in _files(child)]


def test_tree_lists_pdf_docx_and_images_with_kinds(api, vault):
    body = api.as_user("alice").get("/api/personal/vault/tree").json()
    by_path = {f["path"]: f for f in _files(body["tree"])}
    assert {p: f["kind"] for p, f in by_path.items()} == {
        "notes.md": "markdown",
        "logo.png": "image",
        "Clients/Acme/Agreements/msa.docx": "docx",
        "Clients/Acme/Agreements/msa.pdf": "pdf",
    }


def test_raw_pdf_previews_inline_and_downloads_as_attachment(api, vault):
    client = api.as_user("alice")
    inline = client.get("/api/personal/vault/raw", params={"path": "Clients/Acme/Agreements/msa.pdf"})
    assert inline.status_code == 200
    assert inline.content == PDF
    assert inline.headers["content-type"].startswith("application/pdf")
    assert inline.headers["content-disposition"].startswith("inline")
    assert inline.headers["x-content-type-options"] == "nosniff"
    download = client.get(
        "/api/personal/vault/raw", params={"path": "Clients/Acme/Agreements/msa.pdf", "download": "1"}
    )
    assert download.headers["content-disposition"].startswith("attachment")


def test_raw_docx_is_always_an_attachment(api, vault):
    resp = api.as_user("alice").get(
        "/api/personal/vault/raw", params={"path": "Clients/Acme/Agreements/msa.docx"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment")


def test_raw_refuses_unlisted_types(api, vault):
    client = api.as_user("alice")
    for name in ("script.exe", "vector.svg", ".hidden.pdf", "Clients/Acme/Agreements/~$msa.docx"):
        assert client.get("/api/personal/vault/raw", params={"path": name}).status_code in (400, 404), name


def test_editor_still_refuses_non_markdown(api, vault):
    resp = api.as_user("alice").get("/api/personal/vault/file", params={"path": "logo.png"})
    assert resp.status_code == 400


@pytest.mark.security
def test_raw_requires_login(api, vault):
    resp = api.anonymous().get("/api/personal/vault/raw", params={"path": "logo.png"})
    assert resp.status_code in (401, 403)


@pytest.mark.security
def test_raw_refuses_traversal_and_absolute_paths(api, vault, tmp_path):
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(PDF)
    bob = api.as_user("bob")
    for path in ("../secret.pdf", "Clients/../../secret.pdf", "..\secret.pdf", str(outside), "/etc/passwd.pdf"):
        resp = bob.get("/api/personal/vault/raw", params={"path": path})
        assert resp.status_code in (400, 403), path
        assert PDF not in resp.content


@pytest.mark.security
def test_raw_refuses_symlink_escape(api, vault, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.pdf").write_bytes(PDF)
    try:
        os.symlink(outside, vault / "link", target_is_directory=True)
        os.symlink(outside / "leak.pdf", vault / "leak.pdf")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    client = api.as_user("alice")
    for path in ("link/leak.pdf", "leak.pdf"):
        assert client.get("/api/personal/vault/raw", params={"path": path}).status_code == 403, path
    paths = {f["path"] for f in _files(client.get("/api/personal/vault/tree").json()["tree"])}
    assert not any("leak" in p for p in paths)

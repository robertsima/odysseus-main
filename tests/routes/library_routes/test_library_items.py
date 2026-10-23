"""GET /api/library/items and /api/library/projects over HTTP.

A temporary vault with a Projects tree like the real one: agreements with
exports, signed copies, backups, review packets and HTML previews.
"""
import builtins
import io
import os

import pytest

import src.database
import src.library_index as library_index
import src.notes_store as notes_store
import src.rag_sensitivity as sensitivity
from core.middleware import INTERNAL_TOOL_USER
from routes import library_routes
from src.notes_markdown import NoteRecord

PDF = b"%PDF-1.4\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
TT = "AI Mind/Projects/Tall Tree Group"
DA = "Tall Tree Group — Deliverable Agreement 01 — Initial Server Setup"
BACKUP = f"{TT}/Agreements/Backups/20261021-215722-contact-defaults/{DA}.docx"
PACKET = f"{TT}/Agreements/Review Packets/remaining-pdf-migration-20261022T233727Z/{DA}.pdf"


def _touch(root, rel, data=b"x", mtime=None):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture(autouse=True)
def _fresh_cache():
    library_index.clear()
    yield
    library_index.clear()


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    for rel, mtime in [
        (f"{TT}/Agreements/{DA}.docx", 1_700_000_300),
        (f"{TT}/Agreements/{DA}.pdf", 1_700_000_310),
        (f"{TT}/Agreements/{DA}.preview.html", 1_700_000_310),
        (BACKUP, 1_700_000_100),
        (PACKET, 1_700_000_200),
        (f"{TT}/Agreements/MSA_signed.pdf", 1_700_000_400),
        (f"{TT}/Agreements/MSA.docx", 1_700_000_350),
        (f"{TT}/Plan.md", 1_700_000_500),
        (f"{TT}/Plan.docx", 1_700_000_450),
        (f"{TT}/site photo.png", 1_700_000_050),
        ("AI Mind/Projects/Dono Fuel/Quote.md", 1_700_000_600),
        ("AI Mind/Projects/Dono Fuel/Quote_signed.pdf", 1_700_000_610),
        ("Reference/guide.md", 1_700_000_020),
        ("Notes/should-not-be-a-file.md", 1_700_000_999),
        ("~$lock.docx", 1_700_000_000),
    ]:
        data = PNG if rel.endswith(".png") else PDF if rel.endswith(".pdf") else b"x"
        _touch(root, rel, data, mtime)
    monkeypatch.setattr(sensitivity, "vault_root", lambda: str(root))
    monkeypatch.setattr(sensitivity, "_safe_folder_policy_map", lambda: ({}, True))
    monkeypatch.setattr(notes_store, "vault_root", lambda: str(root))
    return root


def _add_document(owner, title, doc_id, content="body", language="markdown"):
    db = src.database.SessionLocal()
    try:
        db.add(src.database.Document(id=doc_id, title=title, current_content=content,
                                     language=language, owner=owner, is_active=True))
        db.commit()
    finally:
        db.close()


def _add_note(owner, title, **fields):
    note = NoteRecord(id=f"n-{title}".replace(" ", "-"), owner=owner, title=title, **fields)
    notes_store.STORE.save(note, enforce_readonly=False)
    return note


def _items(api, user="alice", **params):
    resp = api.as_user(user).get("/api/library/items", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _group(body, stem):
    return next(i for i in body["items"] if i["kind"] == "group" and i["id"].endswith(f"/{stem}"))


def test_agreement_group_carries_master_export_history_and_preview(api, vault):
    body = _items(api, kind="file")
    g = _group(body, DA.lower())
    assert g["title"] == DA
    assert g["role"] == "master" and g["type"] == "docx"
    assert g["project"] == "Tall Tree Group"
    assert g["breadcrumb"] == ["Projects", "Tall Tree Group", "Agreements"]
    assert g["group"]["master"]["path"] == f"{TT}/Agreements/{DA}.docx"
    assert g["group"]["current_export"]["path"] == f"{TT}/Agreements/{DA}.pdf"
    assert [h["path"] for h in g["group"]["history"]] == [PACKET, BACKUP]
    assert [s["title"] for s in g["group"]["supporting"]] == [f"{DA}.preview"]
    assert "history" in g["actions"] and g["locked"] is False
    assert body["vault_shared"] is True


def test_history_and_supporting_files_are_never_top_level(api, vault):
    body = _items(api, kind="all", limit=200)
    assert {i["role"] for i in body["items"]} <= {"master", "export", "signed", "source", None}
    top_paths = {i["path"] for i in body["items"] if i["path"]}
    assert BACKUP not in top_paths and PACKET not in top_paths
    assert not any(p.endswith(".html") for p in top_paths)
    assert not any("lock.docx" in p for p in top_paths)
    assert not any(i["id"].startswith("file:") for i in body["items"])


def test_signed_copy_is_locked_and_markdown_master_is_editable(api, vault):
    body = _items(api, kind="file")
    msa = _group(body, "msa")
    assert msa["group"]["master"]["path"].endswith("MSA.docx")
    assert [s["path"].rsplit("/", 1)[1] for s in msa["group"]["signed"]] == ["MSA_signed.pdf"]
    quote = _group(body, "quote")
    assert quote["role"] == "master" and "edit" in quote["actions"]
    assert quote["links"]["edit"] == "/api/personal/vault/file?path=AI%20Mind/Projects/Dono%20Fuel/Quote.md"
    assert quote["group"]["signed"][0]["locked"] is True
    assert quote["group"]["current_export"] is None


def test_markdown_master_with_newer_word_copy_warns(api, vault):
    body = _items(api, kind="file")
    assert _group(body, "plan")["group"]["warnings"] == []
    os.utime(vault / TT / "Plan.docx", (1_700_009_000, 1_700_009_000))
    library_index.clear()
    assert _group(_items(api, kind="file"), "plan")["group"]["warnings"] == ["word_newer_than_master"]


def test_notes_directory_files_are_not_listed_as_files(api, vault):
    paths = [i["path"] for i in _items(api, kind="file", limit=200)["items"]]
    assert not any(p and p.startswith("Notes/") for p in paths)


def test_file_outside_projects_gets_vault_breadcrumb_and_no_project(api, vault):
    guide = _group(_items(api, kind="file"), "guide")
    assert guide["project"] is None and guide["breadcrumb"] == ["Reference"]


def test_projects_list_counts_documents_files_and_notes(api, vault):
    _add_document("alice", "Tall Tree Group — Statement of work", "d1")
    _add_document("alice", "Dono-Fuel: pricing", "d2")
    _add_note("alice", "Kickoff", label="Tall Tree Group")
    body = api.as_user("alice").get("/api/library/projects").json()
    by_name = {p["name"]: p for p in body["projects"]}
    assert sorted(by_name) == ["Dono Fuel", "Tall Tree Group"]
    assert by_name["Tall Tree Group"]["path"] == TT
    assert by_name["Tall Tree Group"]["counts"] == {"documents": 1, "files": 4, "notes": 1}
    assert by_name["Dono Fuel"]["counts"] == {"documents": 1, "files": 1, "notes": 0}
    assert by_name["Tall Tree Group"]["updated_at"].startswith("2023-")


def test_document_matches_project_by_title_prefix_ignoring_punctuation(api, vault):
    _add_document("alice", "tall tree group - SOW draft", "d1")
    _add_document("alice", "Tall Treehouse plans", "d2")
    _add_document("alice", "Groceries", "d3")
    docs = {i["title"]: i for i in _items(api, kind="document")["items"]}
    assert docs["tall tree group - SOW draft"]["project"] == "Tall Tree Group"
    assert docs["tall tree group - SOW draft"]["breadcrumb"] == ["Projects", "Tall Tree Group"]
    assert docs["Tall Treehouse plans"]["project"] is None
    assert docs["Groceries"]["breadcrumb"] == ["Documents"]
    assert docs["Groceries"]["id"] == "doc:d3"


def test_pdf_wrapper_documents_are_typed_pdf(api, vault):
    _add_document("alice", "Scan", "d1", content='<!-- pdf_source upload_id="u1" -->\ntext')
    (item,) = _items(api, kind="document")["items"]
    assert item["type"] == "pdf"


def test_note_matches_project_by_label_or_project_tag_and_hides_content(api, vault):
    _add_note("alice", "By label", label="dono fuel", content="secret body text")
    _add_note("alice", "By tag", content="notes #project/Tall-Tree-Group follow up")
    _add_note("alice", "Loose", content="nothing here")
    body = _items(api, kind="note")
    notes = {i["title"]: i for i in body["items"]}
    assert notes["By label"]["project"] == "Dono Fuel"
    assert notes["By tag"]["project"] == "Tall Tree Group"
    assert notes["Loose"]["project"] is None
    assert notes["By label"]["id"].startswith("note:")
    assert "secret body text" not in str(body)


def test_query_matches_title_and_breadcrumb_tokens_case_insensitively(api, vault):
    _add_document("alice", "Invoice March", "d1")
    by_title = _items(api, q="INVOICE")["items"]
    assert [i["title"] for i in by_title] == ["Invoice March"]
    by_crumb = _items(api, kind="file", q="agreements tall")["items"]
    assert by_crumb and all("Agreements" in i["breadcrumb"] for i in by_crumb)
    assert _items(api, q="nonexistent-zzz")["total"] == 0


def test_project_filter_and_kind_project(api, vault):
    _add_document("alice", "Dono Fuel invoice", "d1")
    _add_document("alice", "Groceries", "d2")
    dono = _items(api, project="dono fuel", limit=200)["items"]
    assert {i["project"] for i in dono} == {"Dono Fuel"}
    assert {i["kind"] for i in dono} == {"group", "document"}
    in_projects = _items(api, kind="project", limit=200)["items"]
    assert in_projects and all(i["project"] for i in in_projects)
    assert "Groceries" not in {i["title"] for i in in_projects}


def test_all_is_newest_first_and_paginates(api, vault):
    _add_document("alice", "Newest doc", "d1")
    full = _items(api, limit=200)
    stamps = [i["updated_at"] for i in full["items"]]
    assert stamps == sorted(stamps, reverse=True)
    assert full["items"][0]["title"] == "Newest doc"
    assert full["projects"] == ["Dono Fuel", "Tall Tree Group"]
    page = _items(api, limit=2, offset=1)
    assert page["total"] == full["total"]
    assert [i["id"] for i in page["items"]] == [i["id"] for i in full["items"][1:3]]


def test_bad_kind_is_a_400(api, vault):
    assert api.as_user("alice").get("/api/library/items", params={"kind": "bogus"}).status_code == 400


def test_listing_never_opens_vault_files(api, vault, monkeypatch):
    opened = []
    real_open = io.open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    client = api.as_user("alice")
    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", spy)
    client.get("/api/library/items", params={"kind": "all", "limit": 200})
    client.get("/api/library/projects")
    # Notes are read by the notes store (that is where their titles live);
    # no vault file outside the Notes folder is ever opened.
    assert [p for p in opened if str(vault) in p and "Notes" not in p] == []


def test_vault_write_clears_the_cache_but_a_bare_disk_change_waits_for_the_ttl(api, vault):
    client = api.as_user("alice")
    assert not [i for i in _items(api, kind="file", limit=200)["items"] if i["title"] == "On disk"]
    _touch(vault, f"{TT}/On disk.md", b"x")
    assert not [i for i in _items(api, kind="file", limit=200)["items"] if i["title"] == "On disk"]
    created = client.post("/api/personal/vault/file", json={"path": f"{TT}/Fresh note.md", "content": "hi"})
    assert created.status_code == 200, created.text
    titles = {i["title"] for i in _items(api, kind="file", limit=200)["items"]}
    assert {"Fresh note", "On disk"} <= titles


def test_note_save_clears_the_cache(api, vault):
    _items(api, kind="file")
    _touch(vault, f"{TT}/On disk.md", b"x")
    _add_note("alice", "Trigger")
    assert "On disk" in {i["title"] for i in _items(api, kind="file", limit=200)["items"]}


@pytest.mark.security
def test_bob_does_not_see_alices_documents_or_notes(api, vault):
    _add_document("alice", "Alice private plan", "d1")
    _add_note("alice", "Alice diary")
    bob = _items(api, user="bob", limit=200)
    titles = {i["title"] for i in bob["items"]}
    assert "Alice private plan" not in titles and "Alice diary" not in titles
    alice = {i["title"] for i in _items(api, user="alice", limit=200)["items"]}
    assert {"Alice private plan", "Alice diary"} <= alice
    counts = api.as_user("bob").get("/api/library/projects").json()["projects"]
    assert all(p["counts"]["documents"] == 0 and p["counts"]["notes"] == 0 for p in counts)


@pytest.mark.security
def test_anonymous_is_refused(api, vault):
    anon = api.anonymous()
    assert anon.get("/api/library/items").status_code in (401, 403)
    assert anon.get("/api/library/projects").status_code in (401, 403)


@pytest.mark.security
def test_agent_loopback_user_is_rejected():
    from fastapi import HTTPException, Request

    request = Request({"type": "http", "method": "GET", "path": "/api/library/items",
                       "headers": [], "client": ("127.0.0.1", 1)})
    request.state.current_user = INTERNAL_TOOL_USER
    with pytest.raises(HTTPException) as exc:
        library_routes._human_owner(request)
    assert exc.value.status_code == 403


def _paths_of(item):
    if item.get("path"):
        yield item["path"]
    for part in (item.get("group") or {}).values():
        for sub in part if isinstance(part, list) else [part]:
            if isinstance(sub, dict) and sub.get("path"):
                yield sub["path"]


@pytest.mark.security
def test_no_listed_path_escapes_the_vault(api, vault, tmp_path):
    outside = tmp_path / "outside"
    _touch(outside, "secret.docx", b"x")
    try:
        os.symlink(outside, vault / "Reference" / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    paths = [p for item in _items(api, limit=200)["items"] for p in _paths_of(item)]
    assert paths
    for path in paths:
        assert ".." not in path.split("/") and not path.startswith("/") and "\\" not in path
        assert "secret" not in path and "link" not in path.lower().split("/")

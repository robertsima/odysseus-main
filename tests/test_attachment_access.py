"""A chat's attachments are readable by the workers it starts, and only by them.

2026-10-01: the user attached a Penpot export to the admin chat. The admin chat
read it; every worker it delegated to was refused ("outside the workspace and
outside the personal documents directory") and its sandbox could not see the
file either, so one worker asked the user to re-supply a file that was already
on the server. A worker now reads the attachments of its own chat and of every
chat above it, read-only, same owner only.
"""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database
from src import attachment_access, tool_execution

ATT_ID = "75e65fab8ae740b290ac363c83d79363.penpot"
OTHER_ID = "11111111111111111111111111111111.penpot"
FOREIGN_ID = "22222222222222222222222222222222.penpot"
SIBLING_ID = "33333333333333333333333333333333.penpot"


class FakeUploads:
    """Just the read side of UploadHandler that attachment_access uses."""

    def __init__(self, root, rows):
        self.root = os.path.realpath(root)
        self.rows = rows

    def get_upload_info(self, upload_id):
        return self.rows.get(upload_id)

    def _inside_upload_dir(self, path):
        return os.path.realpath(path).startswith(self.root)


def _meta(*ids):
    return json.dumps({"attachments": [{"id": i, "attachment_id": i, "name": "x.penpot"} for i in ids]})


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'chat.db'}")
    tables = [database.Session.__table__, database.ChatMessage.__table__,
              database.ArchivedChatMessage.__table__]
    database.Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine)
    # Patch the factory; reloading core.database breaks later tests.
    monkeypatch.setattr(database, "SessionLocal", factory)

    uploads = tmp_path / "uploads" / "2026" / "10" / "01"
    uploads.mkdir(parents=True)
    files = {}
    rows = {}
    for upload_id, owner in ((ATT_ID, "alice"), (OTHER_ID, "alice"), (FOREIGN_ID, "bob"), (SIBLING_ID, "alice")):
        f = uploads / upload_id
        f.write_bytes(b"PK\x03\x04 zip-ish")
        files[upload_id] = os.path.realpath(f)
        rows[upload_id] = {"id": upload_id, "owner": owner, "path": str(f)}
    handler = FakeUploads(tmp_path / "uploads", rows)
    import src.tool_utils as tool_utils

    monkeypatch.setattr(tool_utils, "get_upload_handler", lambda: handler)
    import src.constants as constants

    monkeypatch.setattr(constants, "UPLOAD_DIR", str(tmp_path / "uploads"))

    def session(sid, owner, parent=None, messages=(), archived=()):
        db = factory()
        db.add(database.Session(
            id=sid, name=sid, endpoint_url="http://x", model="m", owner=owner,
            settings_json=json.dumps({"parent_session": parent}) if parent else None,
        ))
        db.commit()
        for n, (model, metas) in enumerate(((database.ChatMessage, messages), (database.ArchivedChatMessage, archived))):
            for i, meta in enumerate(metas):
                db.add(model(id=f"{sid}-{n}-{i}", session_id=sid, role="user", content="hi", meta_data=meta))
        db.commit()
        db.close()

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("x", encoding="utf-8")
    ws_token = tool_execution._active_workspace.set(os.path.realpath(workspace))
    attachment_access.clear_cache()
    yield SimpleNamespace(files=files, session=session, workspace=workspace, uploads=uploads)
    tool_execution._active_workspace.reset(ws_token)
    attachment_access.clear_cache()


def _as(session_id, owner):
    return attachment_access.bind(session_id, owner)


def test_worker_reads_parent_chat_attachment(world):
    world.session("admin", "alice", messages=[_meta(ATT_ID)])
    world.session("worker", "alice", parent="admin")
    world.session("sub", "alice", parent="worker")  # grandchild: any depth
    for sid in ("worker", "sub"):
        token = _as(sid, "alice")
        try:
            got = tool_execution._resolve_tool_path(
                str(world.files[ATT_ID]), allow_attachment_read=True)
            assert got == world.files[ATT_ID]
        finally:
            attachment_access.unbind(token)


def test_worker_without_a_workspace_reads_parent_chat_attachment(world, monkeypatch):
    # Penpot designers and reviewers often run with no workspace bound.
    world.session("admin", "alice", messages=[_meta(ATT_ID)])
    world.session("designer", "alice", parent="admin")
    monkeypatch.setattr(tool_execution, "_tool_path_roots", lambda: [])
    ws_token = tool_execution._active_workspace.set(None)
    token = _as("designer", "alice")
    try:
        got = tool_execution._resolve_tool_path(
            str(world.files[ATT_ID]), allow_attachment_read=True)
        assert got == world.files[ATT_ID]
        with pytest.raises(ValueError):
            tool_execution._resolve_tool_path(str(world.files[ATT_ID]))
    finally:
        attachment_access.unbind(token)
        tool_execution._active_workspace.reset(ws_token)


def test_attachment_moved_to_archive_by_compaction_is_still_found(world):
    world.session("admin", "alice", archived=[_meta(ATT_ID)])
    world.session("worker", "alice", parent="admin")
    token = _as("worker", "alice")
    try:
        assert attachment_access.current_attachment_paths() == {world.files[ATT_ID]}
    finally:
        attachment_access.unbind(token)


def test_unrelated_chat_and_other_owner_are_refused(world):
    world.session("admin", "alice", messages=[_meta(ATT_ID)])
    world.session("sibling", "alice", messages=[_meta(SIBLING_ID)])  # not an ancestor
    world.session("worker", "alice", parent="admin", messages=[_meta(FOREIGN_ID)])  # bob's upload
    world.session("intruder", "bob", parent="admin")  # forged parent: other owner
    token = _as("worker", "alice")
    try:
        assert attachment_access.current_attachment_paths() == {world.files[ATT_ID]}
        for upload_id in (SIBLING_ID, FOREIGN_ID, OTHER_ID):
            with pytest.raises(ValueError, match="not an attachment of this chat"):
                tool_execution._resolve_tool_path(
                    str(world.files[upload_id]), allow_attachment_read=True)
    finally:
        attachment_access.unbind(token)
    token = _as("intruder", "bob")
    try:
        assert attachment_access.current_attachment_paths() == frozenset()
        with pytest.raises(ValueError, match="outside the workspace"):
            tool_execution._resolve_tool_path(str(world.files[ATT_ID]), allow_attachment_read=True)
    finally:
        attachment_access.unbind(token)


def test_refusal_names_readable_attachments_and_never_other_uploads(world):
    world.session("admin", "alice", messages=[_meta(ATT_ID)])
    world.session("worker", "alice", parent="admin")
    token = _as("worker", "alice")
    try:
        with pytest.raises(ValueError) as err:
            tool_execution._resolve_tool_path(str(world.uploads), allow_attachment_read=True)
        assert world.files[ATT_ID] in str(err.value)
        assert OTHER_ID not in str(err.value) and SIBLING_ID not in str(err.value)
    finally:
        attachment_access.unbind(token)


def test_writes_to_an_attachment_are_refused(world, monkeypatch):
    world.session("admin", "alice", messages=[_meta(ATT_ID)])
    world.session("worker", "alice", parent="admin")
    token = _as("worker", "alice")
    try:
        # Bound to a workspace the write is refused as outside it, and says why.
        with pytest.raises(ValueError, match="attachments are read-only"):
            tool_execution._resolve_tool_path(str(world.files[ATT_ID]))
        # Where a root does reach the upload store (no workspace), the guard
        # itself refuses a write-capable resolve of the same file.
        monkeypatch.setattr(tool_execution, "_resolve_tool_path_unguarded",
                            lambda raw, **kw: os.path.realpath(raw))
        with pytest.raises(ValueError, match="read-only"):
            tool_execution._resolve_tool_path(str(world.files[ATT_ID]))
    finally:
        attachment_access.unbind(token)


def test_write_file_tool_cannot_overwrite_an_attachment(world):
    from src.agent_tools.filesystem_tools import ApplyPatchTool, EditFileTool, WriteFileTool

    world.session("admin", "alice", messages=[_meta(ATT_ID)])
    world.session("worker", "alice", parent="admin")
    path = world.files[ATT_ID]
    before = open(path, "rb").read()
    token = _as("worker", "alice")
    try:
        results = [
            asyncio.run(WriteFileTool().execute(json.dumps({"path": path, "content": "x"}), {})),
            asyncio.run(EditFileTool().execute(json.dumps({"path": path, "old_string": "zip", "new_string": "y"}), {})),
            asyncio.run(ApplyPatchTool().execute(json.dumps({"patch_text": f"*** Begin Patch\n*** Delete File: {path}\n*** End Patch"}), {})),
        ]
    finally:
        attachment_access.unbind(token)
    assert all(r["exit_code"] == 1 and "read-only" in r["error"] for r in results), results
    assert open(path, "rb").read() == before


def test_read_file_tool_reads_the_attachment(world):
    from src.agent_tools.filesystem_tools import ReadFileTool

    world.session("admin", "alice", messages=[_meta(ATT_ID)])
    world.session("worker", "alice", parent="admin")
    token = _as("worker", "alice")
    try:
        out = asyncio.run(ReadFileTool().execute(world.files[ATT_ID], {}))
    finally:
        attachment_access.unbind(token)
    assert out["exit_code"] == 0 and "zip-ish" in out["output"]


def test_sensitive_denials_still_apply_to_an_attachment(world, monkeypatch):
    world.session("admin", "alice", messages=[_meta(ATT_ID)])
    token = _as("admin", "alice")
    monkeypatch.setattr(tool_execution, "_is_sensitive_path", lambda *a, **k: True)
    try:
        with pytest.raises(ValueError, match="sensitive"):
            tool_execution._resolve_tool_path(str(world.files[ATT_ID]), allow_attachment_read=True)
    finally:
        attachment_access.unbind(token)


def test_sandbox_binds_hold_only_lineage_files_and_are_capped(world):
    world.session("admin", "alice", messages=[_meta(ATT_ID)])
    world.session("worker", "alice", parent="admin")
    token = _as("worker", "alice")
    try:
        assert attachment_access.sandbox_read_only_binds() == {
            world.files[ATT_ID]: world.files[ATT_ID]}
        os.remove(world.files[ATT_ID])
        attachment_access.clear_cache()
        assert attachment_access.sandbox_read_only_binds() == {}  # missing files are skipped
    finally:
        attachment_access.unbind(token)


def test_python_tool_passes_attachments_to_the_sandbox(world, monkeypatch):
    from src import shell_sandbox
    from src.agent_tools.subprocess_tools import PythonTool

    world.session("admin", "alice", messages=[_meta(ATT_ID)])
    world.session("worker", "alice", parent="admin")
    seen = {}

    def fake_build_argv(inner, **kwargs):
        seen.update(kwargs)
        return [sys.executable, "-c", "print('ok')"]

    monkeypatch.setattr(shell_sandbox, "build_argv", fake_build_argv)
    ws_token = tool_execution._shell_sandbox_workspace.set(str(world.workspace))
    mode_token = tool_execution._shell_mode_var.set("sandbox")
    token = _as("worker", "alice")
    try:
        asyncio.run(PythonTool().execute("print(1)", {}))
    finally:
        attachment_access.unbind(token)
        tool_execution._shell_mode_var.reset(mode_token)
        tool_execution._shell_sandbox_workspace.reset(ws_token)
    assert seen["extra_ro_binds"] == {world.files[ATT_ID]: world.files[ATT_ID]}


def test_no_session_means_no_attachment_access(world):
    world.session("admin", "alice", messages=[_meta(ATT_ID)])
    assert attachment_access.current_attachment_paths() == frozenset()
    with pytest.raises(ValueError, match="outside the workspace"):
        tool_execution._resolve_tool_path(str(world.files[ATT_ID]), allow_attachment_read=True)

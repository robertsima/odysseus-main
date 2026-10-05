"""Separate read (model) and write (human editor) paths for the vault.

``vault_directory`` is what models, retrieval and the readonly folder policy
see; ``vault_write_directory`` is where the signed-in human's vault editor
saves. A read-only personal_docs mount must not block human saves when a
writable mount of the same vault is configured, and the write path must never
widen what models may write.
"""

import errno
import json

import pytest
from fastapi import HTTPException

import src.rag_sensitivity as sensitivity
from routes import personal_routes


class _Docs:
    def __init__(self, root):
        self.personal_dir = str(root)
        self.index = []
        self.directory_sensitivity = {}
        self.refreshed = 0

    def get_indexed_directories(self):
        return []

    def refresh_index(self):
        self.refreshed += 1


class _Rag:
    def __init__(self):
        self.deleted = []
        self.indexed = []

    def delete_by_source(self, path):
        self.deleted.append(path)
        return 1

    def owner_for_directory(self, _directory):
        return None

    def index_file(self, path, owner=None, sensitivity="public"):
        self.indexed.append(path)
        return 1, 0


def _endpoint(router, path, method):
    return next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set())
    )


@pytest.fixture
def split_vault(tmp_path, monkeypatch):
    """A read view and a separate write view holding the same notes.

    Production mounts one host folder twice (read-only for models, read-write
    for the editor); here two copies stand in for those two mounts.
    """
    read = tmp_path / "personal_docs"
    write = tmp_path / "vault_rw"
    for root in (read, write):
        (root / "Vault Mind").mkdir(parents=True)
        (root / "Vault Mind" / "plan.md").write_text("original\n", encoding="utf-8")

    config = {"write": ""}
    monkeypatch.setattr(sensitivity, "vault_root", lambda: str(read))
    monkeypatch.setattr(sensitivity, "configured_vault_write_directory", lambda: config["write"])
    monkeypatch.setattr(
        sensitivity,
        "_safe_folder_policy_map",
        lambda: ({"Vault Mind": sensitivity.FolderPolicy(readonly=True)}, True),
    )
    rag = _Rag()
    docs = _Docs(read)
    monkeypatch.setattr(personal_routes, "get_rag_manager", lambda: rag)
    router = personal_routes.setup_personal_routes(docs, rag, True)
    return read, write, config, rag, router


def _put(router, path, content, modified=None):
    return _endpoint(router, "/api/personal/vault/file", "PUT")(
        body=personal_routes.VaultFileUpdate(path=path, content=content, modified=modified),
        owner="alice",
    )


def _tree(router):
    return _endpoint(router, "/api/personal/vault/tree", "GET")(owner="alice")


# ── defaults: unset write path saves into personal_docs ─────────────────────

def test_unset_write_path_saves_into_the_read_vault(split_vault):
    read, write, _config, rag, router = split_vault
    assert _tree(router)["write"] == {"separate": False, "writable": True, "reason": ""}

    result = _put(router, "Vault Mind/plan.md", "human edit\n")
    assert result["success"] is True
    # Vault Mind is readonly for models; the human editor still saves.
    assert result["readonly"] is True
    assert (read / "Vault Mind" / "plan.md").read_text(encoding="utf-8") == "human edit\n"
    assert (write / "Vault Mind" / "plan.md").read_text(encoding="utf-8") == "original\n"
    assert rag.indexed == [str(read / "Vault Mind" / "plan.md")]


def test_write_path_equal_to_read_path_is_not_separate(split_vault):
    read, _write, config, _rag, router = split_vault
    config["write"] = str(read)
    assert _tree(router)["write"]["separate"] is False
    _put(router, "Vault Mind/plan.md", "same root\n")
    assert (read / "Vault Mind" / "plan.md").read_text(encoding="utf-8") == "same root\n"


def test_read_only_vault_without_write_path_explains_the_setting(split_vault, monkeypatch):
    read, _write, _config, _rag, router = split_vault
    monkeypatch.setattr(personal_routes, "_vault_dir_writable", lambda _path: False)

    status = _tree(router)["write"]
    assert status["writable"] is False
    assert "Vault write folder" in status["reason"]

    with pytest.raises(HTTPException) as exc:
        _put(router, "Vault Mind/plan.md", "lost?\n")
    assert exc.value.status_code == 409
    assert "Vault write folder" in exc.value.detail
    assert (read / "Vault Mind" / "plan.md").read_text(encoding="utf-8") == "original\n"


def test_erofs_from_the_filesystem_is_a_clear_conflict(split_vault, monkeypatch):
    read, _write, _config, _rag, router = split_vault

    def _erofs(*_args, **_kwargs):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(personal_routes.tempfile, "mkstemp", _erofs)
    with pytest.raises(HTTPException) as exc:
        _put(router, "Vault Mind/plan.md", "x\n")
    assert exc.value.status_code == 409
    assert "Vault write folder" in exc.value.detail
    assert (read / "Vault Mind" / "plan.md").read_text(encoding="utf-8") == "original\n"


# ── a separate write path ───────────────────────────────────────────────────

def test_separate_write_path_receives_human_saves(split_vault, monkeypatch):
    read, write, config, rag, router = split_vault
    config["write"] = str(write)
    # personal_docs is a read-only mount; only the write view is writable.
    real_read = str(read.resolve())
    monkeypatch.setattr(personal_routes, "_vault_dir_writable", lambda path: path != real_read)

    assert _tree(router)["write"] == {"separate": True, "writable": True, "reason": ""}
    opened = _endpoint(router, "/api/personal/vault/file", "GET")(path="Vault Mind/plan.md", owner="alice")
    result = _put(router, "Vault Mind/plan.md", "saved via write mount\n",
                  modified=(write / "Vault Mind" / "plan.md").stat().st_mtime)
    assert opened["content"] == "original\n"
    assert result["success"] is True
    assert (write / "Vault Mind" / "plan.md").read_text(encoding="utf-8") == "saved via write mount\n"
    assert (read / "Vault Mind" / "plan.md").read_text(encoding="utf-8") == "original\n"
    # Chunks are keyed by the read path models cite, never the write path.
    assert rag.indexed == [str(read.resolve() / "Vault Mind" / "plan.md")]


def test_separate_write_path_conflict_check_uses_the_written_file(split_vault):
    _read, write, config, _rag, router = split_vault
    config["write"] = str(write)
    stale = (write / "Vault Mind" / "plan.md").stat().st_mtime - 10
    with pytest.raises(HTTPException) as exc:
        _put(router, "Vault Mind/plan.md", "overwrite\n", modified=stale)
    assert exc.value.status_code == 409
    assert (write / "Vault Mind" / "plan.md").read_text(encoding="utf-8") == "original\n"


def test_separate_write_path_must_mirror_the_vault(split_vault):
    read, write, config, _rag, router = split_vault
    config["write"] = str(write)
    (read / "Vault Mind" / "only-read.md").write_text("x\n", encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        _put(router, "Vault Mind/only-read.md", "y\n")
    assert exc.value.status_code == 409
    assert "not in the vault write folder" in exc.value.detail
    assert not (write / "Vault Mind" / "only-read.md").exists()


def test_create_through_write_path_reports_invisible_note(split_vault):
    read, write, config, rag, router = split_vault
    config["write"] = str(write)
    create = _endpoint(router, "/api/personal/vault/file", "POST")

    result = create(body=personal_routes.VaultFileCreate(path="Vault Mind/new", content="# New\n"), owner="alice")
    assert (write / "Vault Mind" / "new.md").read_text(encoding="utf-8") == "# New\n"
    assert not (read / "Vault Mind" / "new.md").exists()
    # Two unrelated trees: the note is not in the model-readable vault, so it
    # is neither indexed nor reported as visible.
    assert result["visible_in_vault"] is False and result["indexed"] is False
    assert rag.indexed == []

    with pytest.raises(HTTPException) as exc:
        create(body=personal_routes.VaultFileCreate(path="Vault Mind/new.md"), owner="alice")
    assert exc.value.status_code == 409


def test_delete_through_write_path_trashes_inside_it(split_vault):
    read, write, config, rag, router = split_vault
    config["write"] = str(write)
    result = _endpoint(router, "/api/personal/vault/file", "DELETE")(path="Vault Mind/plan.md", owner="alice")
    assert result["trashed_to"] == ".trash/Vault Mind/plan.md"
    assert (write / ".trash" / "Vault Mind" / "plan.md").is_file()
    assert not (read / ".trash").exists()
    assert str(read.resolve() / "Vault Mind" / "plan.md") in rag.deleted


@pytest.mark.parametrize("kind,needle", [
    ("missing", "does not exist"),
    ("nested", "overlaps"),
    ("parent", "overlaps"),
    ("readonly", "read-only"),
])
def test_unusable_write_path_is_refused_with_a_reason(split_vault, monkeypatch, kind, needle):
    read, write, config, _rag, router = split_vault
    config["write"] = {
        "missing": str(write.parent / "not-mounted"),
        "nested": str(read / "Vault Mind"),
        "parent": str(read.parent),
        "readonly": str(write),
    }[kind]
    if kind == "readonly":
        real_write = str(write.resolve())
        monkeypatch.setattr(personal_routes, "_vault_dir_writable", lambda path: path != real_write)

    status = _tree(router)["write"]
    assert status["writable"] is False and needle in status["reason"]
    # The reason names the setting, not a host path.
    assert str(write.parent) not in status["reason"]
    with pytest.raises(HTTPException) as exc:
        _put(router, "Vault Mind/plan.md", "nope\n")
    assert exc.value.status_code == 409 and needle in exc.value.detail
    assert (read / "Vault Mind" / "plan.md").read_text(encoding="utf-8") == "original\n"
    assert (write / "Vault Mind" / "plan.md").read_text(encoding="utf-8") == "original\n"


# ── model write permissions are not broadened ───────────────────────────────

@pytest.fixture
def model_tools(tmp_path, monkeypatch):
    data = tmp_path / "data"
    vault = data / "personal_docs"
    write = tmp_path / "vault_rw"
    for root in (vault, write):
        for folder in ("AI Mind", "Vault Mind"):
            (root / folder).mkdir(parents=True)
            (root / folder / "note.md").write_text("old value\n", encoding="utf-8")
    settings = {
        "vault_directory": str(vault),
        "vault_write_directory": str(write),
        "vault_default_sensitivity": "public",
        "vault_folder_sensitivity": {
            "AI Mind": {"sensitivity": "public", "readonly": False},
            "Vault Mind": {"sensitivity": "public", "readonly": True},
        },
        "tool_path_extra_roots": [],
    }
    monkeypatch.setattr("src.constants.DATA_DIR", str(data), raising=False)
    monkeypatch.setattr("src.constants.PERSONAL_DIR", str(vault), raising=False)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: settings.get(key, default))

    from src.tool_execution import _active_workspace

    # Even with the write mount bound as the workspace, agents may not write it.
    token = _active_workspace.set(str(write))
    yield vault, write
    _active_workspace.reset(token)


def test_write_root_is_readonly_policy_for_agents(model_tools):
    vault, write = model_tools
    assert sensitivity.vault_write_root() == str(write)
    assert sensitivity.path_is_readonly(str(write / "AI Mind" / "note.md")) is True
    assert sensitivity.path_is_readonly(str(write / "brand-new.md")) is True
    # The read path keeps its folder rules unchanged.
    assert sensitivity.path_is_readonly(str(vault / "AI Mind" / "note.md")) is False
    assert sensitivity.path_is_readonly(str(vault / "Vault Mind" / "note.md")) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("root_name,folder,ok", [
    ("vault", "AI Mind", True),
    ("vault", "Vault Mind", False),
    ("write", "AI Mind", False),
    ("write", "Vault Mind", False),
])
async def test_model_file_tools_cannot_use_the_write_path(model_tools, root_name, folder, ok):
    from src.agent_tools.filesystem_tools import EditFileTool, WriteFileTool

    vault, write = model_tools
    root = {"vault": vault, "write": write}[root_name]
    path = root / folder / "note.md"
    edit = await EditFileTool().execute(json.dumps({
        "path": str(path), "old_string": "old value", "new_string": "agent value",
    }), {})
    created = root / folder / "agent.md"
    wrote = await WriteFileTool().execute(json.dumps({"path": str(created), "content": "x\n"}), {})
    assert (edit["exit_code"] == 0) is ok
    assert (wrote["exit_code"] == 0) is ok
    assert ("agent value" in path.read_text(encoding="utf-8")) is ok
    assert created.exists() is ok
    if not ok:
        # Refused by the vault readonly policy, not by some unrelated path rule.
        assert "readonly" in edit["error"] and "readonly" in wrote["error"]


def test_shell_workspace_may_not_overlap_the_write_path(model_tools, monkeypatch):
    import src.shell_sandbox as sb

    _vault, write = model_tools
    assert "vault" in sb.workspace_problem(str(write / "AI Mind"))


@pytest.mark.asyncio
async def test_agent_settings_tool_cannot_repoint_the_write_path(monkeypatch):
    import core.database as database
    import src.settings as settings
    from src.agent_tools.admin_tools import do_manage_settings

    class FakeDb:
        def close(self):
            pass

    saved = []
    monkeypatch.setattr(database, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(settings, "load_settings", lambda: {})
    monkeypatch.setattr(settings, "save_settings", lambda value: saved.append(value))
    result = await do_manage_settings(
        json.dumps({"action": "set", "key": "vault_write_directory", "value": "/tmp/anywhere"}),
        owner="admin",
    )
    assert "only be changed by the user" in result["response"]
    assert saved == []


# ── Settings ────────────────────────────────────────────────────────────────

def test_write_path_setting_is_declared_separately_and_defaults_empty():
    from src import settings_schema
    from src.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["vault_write_directory"] == ""
    assert DEFAULT_SETTINGS["vault_directory"] == ""
    spec = settings_schema.get_spec("vault_write_directory")
    assert spec is not None and spec.type == "path" and spec.group == "Knowledge"
    assert spec.label != settings_schema.get_spec("vault_directory").label
    assert settings_schema.normalize_value("vault_write_directory", "") == ""
    assert settings_schema.normalize_value("vault_write_directory", " /mnt/vault ") == "/mnt/vault"
    with pytest.raises(ValueError):
        settings_schema.normalize_value("vault_write_directory", "relative/vault")


def test_write_path_setting_belongs_to_the_vault_capability():
    import src.capabilities_builtin  # noqa: F401  (registers capabilities)
    from src.capabilities import get

    vault = get("vault")
    assert "vault_directory" in vault.settings
    assert "vault_write_directory" in vault.settings

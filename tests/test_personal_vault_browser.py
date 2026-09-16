import pytest
from fastapi import HTTPException
from starlette.requests import Request

from routes import personal_routes
from core.middleware import INTERNAL_TOOL_USER
import src.rag_sensitivity as sensitivity


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
        self.indexed.append((path, owner, sensitivity))
        return 1, 0


def _endpoint(router, path, method):
    return next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set())
    )


@pytest.fixture
def vault_routes(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Private").mkdir(parents=True)
    (vault / "Reference").mkdir()
    (vault / "Private" / "journal.md").write_text("private journal", encoding="utf-8")
    (vault / "Reference" / "guide.md").write_text("read only for agents", encoding="utf-8")
    (vault / "ignore.txt").write_text("not markdown", encoding="utf-8")

    monkeypatch.setattr(sensitivity, "vault_root", lambda: str(vault))
    monkeypatch.setattr(
        sensitivity,
        "_safe_folder_policy_map",
        lambda: (
            {
                "Private": sensitivity.FolderPolicy(sensitivity="private"),
                "Reference": sensitivity.FolderPolicy(readonly=True),
            },
            True,
        ),
    )
    docs = _Docs(vault)
    rag = _Rag()
    monkeypatch.setattr(personal_routes, "get_rag_manager", lambda: rag)
    router = personal_routes.setup_personal_routes(docs, rag, True)
    return vault, docs, rag, router


def test_tree_lists_all_markdown_with_llm_policy_badges(vault_routes):
    _vault, _docs, _rag, router = vault_routes
    result = _endpoint(router, "/api/personal/vault/tree", "GET")(owner="alice")

    def files(node):
        if node["type"] == "file":
            return [node]
        return [item for child in node.get("children", []) for item in files(child)]

    by_path = {item["path"]: item for item in files(result["tree"])}
    assert set(by_path) == {"Private/journal.md", "Reference/guide.md"}
    assert by_path["Private/journal.md"]["sensitivity"] == "private"
    assert by_path["Reference/guide.md"]["readonly"] is True
    assert result["policy_scope"] == "llm_only"


def test_human_can_open_and_edit_private_readonly_vault_file(vault_routes):
    vault, docs, rag, router = vault_routes
    get_file = _endpoint(router, "/api/personal/vault/file", "GET")
    put_file = _endpoint(router, "/api/personal/vault/file", "PUT")

    opened = get_file(path="Reference/guide.md", owner="alice")
    assert opened["content"] == "read only for agents"
    assert opened["readonly"] is True

    result = put_file(
        body=personal_routes.VaultFileUpdate(
            path="Reference/guide.md",
            content="edited by the human",
            modified=opened["modified"],
        ),
        owner="alice",
    )
    assert result["success"] is True
    assert (vault / "Reference" / "guide.md").read_text(encoding="utf-8") == "edited by the human"
    assert rag.deleted and rag.indexed
    assert docs.refreshed == 1


def test_vault_browser_rejects_traversal(vault_routes):
    _vault, _docs, _rag, router = vault_routes
    get_file = _endpoint(router, "/api/personal/vault/file", "GET")
    with pytest.raises(HTTPException) as exc:
        get_file(path="../outside.md", owner="alice")
    assert exc.value.status_code == 403


def test_vault_editor_rejects_internal_agent_identity(vault_routes):
    _vault, _docs, _rag, router = vault_routes
    route = next(
        route for route in router.routes
        if getattr(route, "path", "") == "/api/personal/vault/tree"
    )
    owner_dependency = next(dep.call for dep in route.dependant.dependencies if dep.name == "owner")
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/personal/vault/tree",
        "headers": [],
        "client": ("127.0.0.1", 1234),
    })
    request.state.current_user = INTERNAL_TOOL_USER
    with pytest.raises(HTTPException) as exc:
        owner_dependency(request)
    assert exc.value.status_code == 403


def test_vault_editor_detects_external_changes(vault_routes):
    vault, _docs, _rag, router = vault_routes
    get_file = _endpoint(router, "/api/personal/vault/file", "GET")
    put_file = _endpoint(router, "/api/personal/vault/file", "PUT")
    opened = get_file(path="Reference/guide.md", owner="alice")
    target = vault / "Reference" / "guide.md"
    target.write_text("changed in Obsidian", encoding="utf-8")
    # Some filesystems have coarse timestamps, so make the conflicting value
    # unambiguous instead of relying on a sub-second write distinction.
    stale_mtime = opened["modified"] - 10

    with pytest.raises(HTTPException) as exc:
        put_file(
            body=personal_routes.VaultFileUpdate(
                path="Reference/guide.md",
                content="would overwrite it",
                modified=stale_mtime,
            ),
            owner="alice",
        )
    assert exc.value.status_code == 409
    assert target.read_text(encoding="utf-8") == "changed in Obsidian"

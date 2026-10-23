"""The vault tree labels each file with its Library role."""
import pytest

import src.rag_sensitivity as sensitivity


def _files(node):
    if node["type"] == "file":
        return [node]
    return [f for child in node.get("children", []) for f in _files(child)]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    base = root / "Projects" / "Tall Tree Group" / "Agreements"
    for rel in ("DA.md", "DA.docx", "DA.pdf", "MSA_signed.pdf", "Backups/old/DA.docx", "logo.png"):
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    monkeypatch.setattr(sensitivity, "vault_root", lambda: str(root))
    monkeypatch.setattr(sensitivity, "_safe_folder_policy_map", lambda: ({}, True))
    return root


def test_tree_file_nodes_carry_their_role(api, vault):
    body = api.as_user("alice").get("/api/personal/vault/tree").json()
    roles = {f["name"] + ("@old" if "/Backups/" in f["path"] else ""): f["role"] for f in _files(body["tree"])}
    assert roles == {
        "DA.md": "master",
        "DA.docx": "export",       # a Markdown master sits beside it
        "DA.pdf": "export",
        "MSA_signed.pdf": "signed",
        "DA.docx@old": "history",
        "logo.png": "source",
    }

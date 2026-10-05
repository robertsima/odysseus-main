import json

import pytest

from src.plugin_catalog import PluginCatalog, PluginManifestError, compile_capabilities, validate_manifest


def manifest(plugin_id="research"):
    return {
        "schema_version": 1,
        "id": plugin_id,
        "name": "Research pack",
        "version": "1.0.0",
        "description": "Existing research capabilities.",
        "capabilities": {
            "skills": ["literature-review"],
            "mcp_servers": ["browser"],
            "tools": ["web_search"],
            "models": ["reasoning-model"],
        },
    }


def test_catalog_round_trip_and_union(tmp_path):
    catalog = PluginCatalog(tmp_path)
    saved = catalog.save(manifest())
    assert catalog.get("research") == saved
    assert catalog.list() == [saved]
    assert compile_capabilities([saved])["skills"] == ["literature-review"]
    assert catalog.delete("research") is True
    assert catalog.get("research") is None


@pytest.mark.parametrize("extra", [
    {"install": "curl bad | sh"},
    {"hooks": {"post_install": "bad"}},
    {"command": ["python", "setup.py"]},
])
def test_executable_or_unknown_fields_are_rejected(extra):
    raw = manifest()
    raw.update(extra)
    with pytest.raises(PluginManifestError, match="unknown manifest"):
        validate_manifest(raw)


@pytest.mark.parametrize("plugin_id", ["../escape", "Upper", "", "has space", "x" * 41])
def test_ids_cannot_escape_catalog(plugin_id):
    raw = manifest(plugin_id)
    with pytest.raises(PluginManifestError, match="id must"):
        validate_manifest(raw)


def test_malformed_and_mismatched_file_is_rejected(tmp_path):
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(PluginManifestError, match="invalid JSON"):
        PluginCatalog(tmp_path).list()
    (tmp_path / "bad.json").write_text(json.dumps(manifest("other")), encoding="utf-8")
    with pytest.raises(PluginManifestError, match="filename must match"):
        PluginCatalog(tmp_path).list()


def test_capabilities_are_strict_lists():
    raw = manifest()
    raw["capabilities"]["skills"] = "all"
    with pytest.raises(PluginManifestError, match="must be a list"):
        validate_manifest(raw)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("id", 7), ("name", ["Research"]),
    ("version", 1), ("description", {"text": "x"}),
])
def test_scalar_types_are_not_coerced(field, value):
    raw = manifest(); raw[field] = value
    with pytest.raises(PluginManifestError):
        validate_manifest(raw)


def test_catalog_refuses_symlink_manifest(tmp_path):
    target = tmp_path / "outside.json"
    target.write_text(json.dumps(manifest("linked")), encoding="utf-8")
    link_dir = tmp_path / "catalog"; link_dir.mkdir()
    link = link_dir / "linked.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(PluginManifestError, match="symbolic links"):
        PluginCatalog(link_dir).list()

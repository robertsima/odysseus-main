"""Skill tiers (2026-10-01): core auto-installs; curated is opt-in; integration
skills belong to an integration package and are hidden without it."""
import json
import textwrap
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services.memory.skill_format import Skill
from services.memory.skills import SkillsManager
from src import builtin_skills


def _skill_md(name: str, body: str = "Body.", extra_fm: str = "") -> str:
    return f"---\nname: {name}\ndescription: Use for {name}.\n{extra_fm}---\n\n{body}"


@pytest.fixture
def app_root(tmp_path, monkeypatch):
    root = tmp_path / "app"
    for category, name in (("general", "core-one"), ("dev", "cur-one"), ("dev", "cur-two")):
        d = root / "skills" / category / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(_skill_md(name), encoding="utf-8")
        (d / "references").mkdir()
        (d / "references" / "r.md").write_text("ref", encoding="utf-8")
    (root / "skills" / "catalog.json").write_text(json.dumps({"version": 1, "skills": [
        {"name": "core-one", "category": "general", "tier": "core", "tags": ["a"]},
        {"name": "cur-one", "category": "dev", "tier": "curated", "tags": ["b"],
         "summary": "First curated.", "license": "MIT", "source": "upstream/x"},
        {"name": "cur-two", "category": "dev", "tier": "curated", "summary": "Second."},
        {"tier": "bogus", "name": "ignored"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(builtin_skills, "get_app_root", lambda: str(root))
    monkeypatch.setattr(builtin_skills, "_customised_logged", set())
    monkeypatch.setattr(builtin_skills, "_integration_skill_dirs", {})
    return root


def _history(root: Path, **bodies):
    digests = {n: [builtin_skills.skill_digest(_skill_md(n, b)) for b in bs] for n, bs in bodies.items()}
    (root / "skills" / ".bundled-history.json").write_text(json.dumps(digests), encoding="utf-8")


def _installed(sm, name):
    return next((s for s in sm.load_all() if s["name"] == name), None)


def test_fresh_deployment_installs_only_core(app_root, tmp_path):
    sm = SkillsManager(str(tmp_path / "data"))
    assert builtin_skills.seed_bundled_skills(sm) == ["core-one"]
    assert _installed(sm, "core-one")["source"] == "bundled"
    assert _installed(sm, "cur-one") is None and _installed(sm, "cur-two") is None


def test_existing_bundled_installs_are_restamped_not_deleted(app_root, tmp_path, caplog):
    caplog.set_level("INFO")
    sm = SkillsManager(str(tmp_path / "data"))
    # An owner's server: everything was installed as bundled and ownerless.
    for category, name in (("general", "core-one"), ("dev", "cur-one"), ("dev", "penpot-design-workflow"),
                           ("dev", "todoist-planning")):
        sm._write_skill_at(Skill(name=name, description="d", category=category, source="bundled",
                                 status="published", requires_toolsets=["mcp__x"]),
                           sm._skill_file(category, name))
    # An operator's own skill that happens to share a curated name must not move.
    sm._write_skill_at(Skill(name="cur-two", description="mine", category="dev", source="user", owner="alice"),
                       sm._skill_file("dev", "cur-two"))

    builtin_skills.seed_bundled_skills(sm)

    assert _installed(sm, "core-one")["source"] == "bundled"
    cur = _installed(sm, "cur-one")
    assert (cur["source"], cur["owner"]) == ("curated", None)
    pen = _installed(sm, "penpot-design-workflow")
    assert (pen["source"], pen["requires_integration"]) == ("integration", "penpot")
    assert _installed(sm, "todoist-planning")["requires_integration"] == "todoist"
    own = _installed(sm, "cur-two")
    assert (own["source"], own["owner"]) == ("user", "alice")
    assert "Re-stamped installed skill cur-one as curated" in caplog.text
    # Still usable by every signed-in user, and protected like bundled skills.
    assert {"cur-one", "penpot-design-workflow"} <= {s["name"] for s in sm.load(owner="bob")}
    assert sm.update_skill("cur-one", {"description": "x"}) is False
    assert sm.delete_skill("cur-one") is False
    assert sm.read_skill_md("cur-one", owner="bob")
    # Idempotent.
    builtin_skills.seed_bundled_skills(sm)
    assert _installed(sm, "cur-one")["source"] == "curated"


def test_installed_curated_skill_is_upgraded_when_unedited(app_root, tmp_path):
    _history(app_root, **{"cur-one": ["Old.", "Body."]})
    sm = SkillsManager(str(tmp_path / "data"))
    builtin_skills.install_curated_skill(sm, "cur-one")
    path = Path(_installed(sm, "cur-one")["path"])
    old = Skill.from_markdown(_skill_md("cur-one", "Old."))
    old.source, old.category = "curated", "dev"
    path.write_text(old.to_markdown(), encoding="utf-8")

    builtin_skills.seed_bundled_skills(sm)

    assert "Body." in _installed(sm, "cur-one")["body_extra"]
    assert _installed(sm, "cur-one")["source"] == "curated"


def test_catalog_list_install_uninstall(app_root, tmp_path):
    _history(app_root, **{"cur-one": ["Body."]})
    sm = SkillsManager(str(tmp_path / "data"))
    rows = {r["name"]: r for r in builtin_skills.curated_catalog(sm)}
    assert set(rows) == {"cur-one", "cur-two"}  # core is not offered
    assert rows["cur-one"]["installed"] is False and rows["cur-one"]["license"] == "MIT"

    builtin_skills.install_curated_skill(sm, "cur-one")
    installed = _installed(sm, "cur-one")
    assert (installed["source"], installed["owner"], installed["status"]) == ("curated", None, "published")
    assert Path(installed["path"]).parent.joinpath("references", "r.md").is_file()
    assert {r["name"]: r for r in builtin_skills.curated_catalog(sm)}["cur-one"]["state"] == "current"
    with pytest.raises(builtin_skills.SkillCatalogError) as exc:
        builtin_skills.install_curated_skill(sm, "cur-one")
    assert exc.value.status == 409
    with pytest.raises(builtin_skills.SkillCatalogError) as exc:
        builtin_skills.install_curated_skill(sm, "core-one")
    assert exc.value.status == 404

    sm.record_use("cur-one")
    result = builtin_skills.uninstall_curated_skill(sm, "cur-one")
    assert result["installed"] is False
    assert _installed(sm, "cur-one") is None
    assert "cur-one" not in sm._load_usage()
    # A restart does not bring it back.
    builtin_skills.seed_bundled_skills(sm)
    assert _installed(sm, "cur-one") is None


def test_uninstall_refuses_edited_copy_or_keeps_it_as_own(app_root, tmp_path):
    _history(app_root, **{"cur-one": ["Body."]})
    sm = SkillsManager(str(tmp_path / "data"))
    builtin_skills.install_curated_skill(sm, "cur-one")
    path = Path(_installed(sm, "cur-one")["path"])
    sk = Skill.from_markdown(path.read_text(encoding="utf-8"))
    sk.body_extra = "My edit."
    path.write_text(sk.to_markdown(), encoding="utf-8")

    assert {r["name"]: r for r in builtin_skills.curated_catalog(sm)}["cur-one"]["state"] == "edited"
    with pytest.raises(builtin_skills.SkillCatalogError) as exc:
        builtin_skills.uninstall_curated_skill(sm, "cur-one")
    assert (exc.value.status, exc.value.code) == (409, "edited")
    assert path.is_file()

    kept = builtin_skills.uninstall_curated_skill(sm, "cur-one", keep=True, keep_for="robert")
    assert kept["kept"] is True
    row = _installed(sm, "cur-one")
    assert (row["source"], row["owner"]) == ("user", "robert")
    assert "My edit." in row["body_extra"]
    # Now it is the owner's: editable, and no longer a catalog install.
    assert sm.update_skill("cur-one", {"description": "mine"}, owner="robert") is True
    with pytest.raises(builtin_skills.SkillCatalogError) as exc:
        builtin_skills.uninstall_curated_skill(sm, "cur-one")
    assert exc.value.code == "conflict"


def test_register_integration_skills_installs_and_gates(app_root, tmp_path):
    pkg = tmp_path / "integrations" / "penpot" / "skills"
    (pkg / "penpot-sample").mkdir(parents=True)
    (pkg / "penpot-sample" / "SKILL.md").write_text(
        _skill_md("penpot-sample", extra_fm="status: published\ntags: [design]\n"), encoding="utf-8")
    sm = SkillsManager(str(tmp_path / "data"))
    builtin_skills.register_integration_skills("penpot", [pkg])

    assert "penpot-sample" in builtin_skills.seed_bundled_skills(sm)
    row = _installed(sm, "penpot-sample")
    assert (row["source"], row["requires_integration"], row["owner"]) == ("integration", "penpot", None)
    assert row["tags"] == ["design"]
    assert builtin_skills.is_shipped_skill(row)

    names = lambda **kw: {s["name"] for s in sm.index_for(**kw)}  # noqa: E731
    assert "penpot-sample" in names()                                       # unknown: not hidden
    assert "penpot-sample" in names(available_integrations={"penpot"})
    assert "penpot-sample" not in names(available_integrations=set())
    assert "penpot-sample" not in names(available_integrations={"todoist"})
    assert "core-one" in names(available_integrations=set())               # no requirement: always visible


def test_get_relevant_skills_and_load_honour_available_integrations(tmp_path):
    sm = SkillsManager(str(tmp_path))
    skills = [
        {"name": "design-flow", "description": "design mockups in penpot", "status": "published",
         "requires_integration": "penpot", "tags": ["penpot"]},
        {"name": "plain", "description": "design mockups in penpot", "status": "published"},
    ]
    q = "design mockups in penpot"
    assert {s["name"] for s in sm.get_relevant_skills(q, skills)} == {"design-flow", "plain"}
    assert {s["name"] for s in sm.get_relevant_skills(q, skills, available_integrations={"penpot"})} == {"design-flow", "plain"}
    assert {s["name"] for s in sm.get_relevant_skills(q, skills, available_integrations=set())} == {"plain"}
    sm._write_skill_at(Skill(name="g", description="d", requires_integration="penpot", source="integration"),
                       sm._skill_file("general", "g"))
    assert sm.load(available_integrations=set()) == []
    assert [s["name"] for s in sm.load(available_integrations={"penpot"})] == ["g"]


def test_requires_integration_round_trips_and_is_not_prompt_visible():
    sk = Skill(name="x", description="d", requires_integration="penpot")
    back = Skill.from_markdown(sk.to_markdown())
    assert back.requires_integration == "penpot"
    assert "requires_integration" not in builtin_skills._PROMPT_VISIBLE_FIELDS
    assert Skill.from_markdown(Skill(name="x", description="d").to_markdown()).requires_integration is None


def test_nested_metadata_block_is_read():
    text = textwrap.dedent("""\
        ---
        name: nested
        description: d
        metadata:
          version: 2.1.0
          category: dev
          status: published
          source: curated
          tags: [a, b]
          platforms:
            - linux
          requires_toolsets: [t1]
          requires_integration: todoist
        ---

        body
        """)
    sk = Skill.from_markdown(text)
    assert (sk.version, sk.category, sk.status, sk.source) == ("2.1.0", "dev", "published", "curated")
    assert sk.tags == ["a", "b"] and sk.platforms == ["linux"] and sk.requires_toolsets == ["t1"]
    assert sk.requires_integration == "todoist"
    # A top-level key wins over the nested one.
    top = Skill.from_markdown(text.replace("description: d\n", "description: d\nstatus: draft\n", 1))
    assert top.status == "draft"
    # The real harness skill keeps its metadata nested.
    path = Path(__file__).resolve().parents[1] / "skills" / "general" / "harness-context-and-tool-routing" / "SKILL.md"
    harness = Skill.from_markdown(path.read_text(encoding="utf-8"))
    assert (harness.status, harness.category) == ("published", "general")


def test_ownerless_shipped_skill_usage_is_written_where_it_is_read(tmp_path):
    sm = SkillsManager(str(tmp_path))
    sm._write_skill_at(Skill(name="shared", description="d", source="curated", status="published"),
                       sm._skill_file("general", "shared"))
    sm._write_skill_at(Skill(name="mine", description="d", owner="alice", status="published"),
                       sm._skill_file("general", "mine"))

    sm.set_audit("shared", "pass", owner="alice")
    sm.set_necessity("shared", True, owner="alice")
    sm.record_use("shared", owner="alice")
    sm.set_audit("mine", "pass", owner="alice")

    shared = _installed(sm, "shared")
    assert shared["audit_verdict"] == "pass" and shared["uses"] == 1 and shared["necessity"]["necessary"] is True
    assert _installed(sm, "mine")["audit_verdict"] == "pass"
    usage = sm._load_usage()
    assert "shared" in usage and "alice::shared" not in usage and "alice::mine" in usage


# --- Routes -----------------------------------------------------------------

@pytest.fixture
def client(app_root, tmp_path, monkeypatch):
    import routes.skills_routes as skills_routes
    _history(app_root, **{"cur-one": ["Body."]})
    sm = SkillsManager(str(tmp_path / "data"))
    state = {"admin": True, "user": "robert"}

    def require_admin(request):
        if not state["admin"]:
            raise HTTPException(403, "Admin only")

    monkeypatch.setattr(skills_routes, "require_admin", require_admin)
    monkeypatch.setattr(skills_routes, "get_current_user", lambda request: state["user"])
    app = FastAPI()
    app.include_router(skills_routes.setup_skills_routes(sm))
    test_client = TestClient(app)
    test_client.state, test_client.sm = state, sm
    return test_client


def test_catalog_routes(client):
    listing = client.get("/api/skills/catalog").json()
    assert [s["name"] for s in listing["skills"]] == ["cur-one", "cur-two"]
    assert not any(s["installed"] for s in listing["skills"])

    assert client.post("/api/skills/catalog/cur-one/install").json()["installed"] is True
    assert client.post("/api/skills/catalog/cur-one/install").status_code == 409
    assert client.post("/api/skills/catalog/nope/install").status_code == 404
    assert any(s["name"] == "cur-one" and s["installed"] for s in client.get("/api/skills/catalog").json()["skills"])

    # The skill detail routes still resolve (the literal /catalog route does not shadow them).
    assert client.get("/api/skills/cur-one").json()["source"] == "curated"

    path = Path(client.sm.load_all()[0]["path"])
    sk = Skill.from_markdown(path.read_text(encoding="utf-8"))
    sk.body_extra = "edited"
    path.write_text(sk.to_markdown(), encoding="utf-8")
    refused = client.delete("/api/skills/catalog/cur-one")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "edited"
    kept = client.delete("/api/skills/catalog/cur-one?keep=true").json()
    assert kept["kept"] is True
    assert client.get("/api/skills/cur-one").json()["owner"] == "robert"


def test_catalog_install_and_uninstall_are_admin_only_but_listing_is_not(client):
    client.state["admin"] = False
    assert client.get("/api/skills/catalog").status_code == 200
    assert client.post("/api/skills/catalog/cur-one/install").status_code == 403
    assert client.delete("/api/skills/catalog/cur-one").status_code == 403


# ── integration packages carry their own skills (2026-10-01) ───────────────────

_PACKAGE_SKILLS = {
    "penpot": {"penpot-design-workflow"},
    "claude-code": {"claude-code-delegation"},
    "pi-worker": {"local-pi-delegation"},
    "todoist": {"todoist-planning", "todoist-retrospective"},
}


def test_manifests_ship_their_skills_and_the_seeder_finds_them():
    from src import builtin_skills, integration_registry

    root = Path(__file__).resolve().parents[1]
    builtin_skills._integration_skill_dirs.clear()
    try:
        for integration in integration_registry.all():
            builtin_skills.register_integration_skills(integration.id, integration_registry.skill_dirs(integration.id))
        specs = {s["name"]: s for s in builtin_skills._integration_specs(str(root))}
    finally:
        builtin_skills._integration_skill_dirs.clear()
    for integration_id, names in _PACKAGE_SKILLS.items():
        assert {n for n, s in specs.items() if s["integration"] == integration_id and s["install"]} == names
        for name in names:
            assert Path(specs[name]["source_dir"]).parts[-4:-2] == ("integrations", integration_id)
    # The old locations are gone: one copy per skill.
    for name in ("penpot-design-workflow", "claude-code-delegation", "local-pi-delegation",
                 "todoist-planning", "todoist-retrospective"):
        assert not list((root / "skills").glob(f"**/{name}/SKILL.md"))


def test_harness_skill_no_longer_names_todoist_or_lotus():
    path = Path(__file__).resolve().parents[1] / "skills" / "general" / "harness-context-and-tool-routing" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert "Todoist" not in text and "Lotus" not in text
    from src import integration_registry

    assert "Todoist" in integration_registry.get("todoist").prompt
    assert "Lotus" in integration_registry.get("lotus").prompt

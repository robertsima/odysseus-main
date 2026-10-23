"""The cached vault walk and the project rules that sit on top of it."""
import threading

import pytest

import src.library_index as library_index

KIND = lambda name: "markdown" if name.endswith(".md") else None  # noqa: E731


@pytest.fixture(autouse=True)
def _fresh():
    library_index.clear()
    yield
    library_index.clear()


def _tree(tmp_path, *rels):
    for rel in rels:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    return str(tmp_path)


def test_scan_is_reused_until_ttl_then_refreshed(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(library_index, "_clock", lambda: now[0])
    root = _tree(tmp_path, "a.md")
    first = library_index.scan_vault(root, KIND)
    (tmp_path / "b.md").write_text("x", encoding="utf-8")
    assert library_index.scan_vault(root, KIND) is first
    now[0] += library_index.CACHE_TTL_SECONDS + 1
    assert {f["path"] for f in library_index.scan_vault(root, KIND).files} == {"a.md", "b.md"}


def test_clear_forces_a_fresh_walk(tmp_path):
    root = _tree(tmp_path, "a.md")
    library_index.scan_vault(root, KIND)
    (tmp_path / "b.md").write_text("x", encoding="utf-8")
    library_index.clear()
    assert {f["path"] for f in library_index.scan_vault(root, KIND).files} == {"a.md", "b.md"}


def test_two_vault_roots_do_not_share_a_cache_entry(tmp_path):
    one = _tree(tmp_path / "one", "a.md")
    two = _tree(tmp_path / "two", "b.md")
    assert [f["path"] for f in library_index.scan_vault(one, KIND).files] == ["a.md"]
    assert [f["path"] for f in library_index.scan_vault(two, KIND).files] == ["b.md"]


def test_hidden_and_excluded_folders_are_skipped(tmp_path):
    root = _tree(tmp_path, ".trash/x.md", "node_modules/y.md", "Notes/n.md", "Docs/d.md")
    scan = library_index.scan_vault(root, KIND, exclude_dirs=["notes"])
    assert [f["path"] for f in scan.files] == ["Docs/d.md"]


def test_concurrent_callers_get_a_complete_scan(tmp_path):
    root = _tree(tmp_path, *[f"d{i}/f{i}.md" for i in range(20)])
    results = []

    def run():
        results.append(len(library_index.scan_vault(root, KIND).files))

    threads = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [20] * 8


def test_projects_are_children_of_a_projects_folder_up_to_depth_four(tmp_path):
    root = _tree(
        tmp_path,
        "projects/Alpha/a.md",
        "AI Mind/Projects/Beta/Sub/b.md",
        "a/b/c/PROJECTS/Gamma/g.md",   # Projects at depth 4: allowed
        "a/b/c/d/Projects/Delta/d.md",  # depth 5: too deep
        "Elsewhere/Epsilon/e.md",
    )
    scan = library_index.scan_vault(root, KIND)
    names = sorted(p.rsplit("/", 1)[1] for p in library_index.project_dirs(scan))
    assert names == ["Alpha", "Beta", "Gamma"]
    assert library_index.locate("AI Mind/Projects/Beta/Sub", scan) == ("Beta", ["Projects", "Beta", "Sub"])
    assert library_index.locate("a/b/c/d/Projects/Delta", scan) == (None, ["a", "b", "c", "d", "Projects", "Delta"])
    assert library_index.locate("AI Mind/Projects", scan) == (None, ["Projects"])
    assert library_index.locate("Elsewhere/Epsilon", scan) == (None, ["Elsewhere", "Epsilon"])


def test_project_with_no_files_still_listed(tmp_path):
    root = _tree(tmp_path, "Projects/Full/a.md")
    (tmp_path / "Projects" / "Empty").mkdir()
    scan = library_index.scan_vault(root, KIND)
    assert [p["name"] for p in library_index.project_summaries(scan)] == ["Empty", "Full"]


@pytest.mark.parametrize("title,expected", [
    ("Tall Tree Group — SOW", "Tall Tree Group"),
    ("tall-tree group", "Tall Tree Group"),
    ("Tall Tree Group", "Tall Tree Group"),
    ("Tall Treehouse", None),
    ("Dono Fuel Ltd invoice", "Dono Fuel Ltd"),
    ("Untitled", None),
])
def test_document_title_to_project(title, expected):
    names = ["Tall Tree Group", "Dono Fuel", "Dono Fuel Ltd"]
    assert library_index.project_for_title(title, names) == expected


def test_note_to_project_by_label_and_tags():
    names = ["Tall Tree Group", "Dono Fuel"]
    assert library_index.project_for_note("Dono Fuel", None, None, names) == "Dono Fuel"
    assert library_index.project_for_note(None, ["project/Tall-Tree-Group"], None, names) == "Tall Tree Group"
    assert library_index.project_for_note(None, "#project/dono-fuel, misc", None, names) == "Dono Fuel"
    assert library_index.project_for_note(None, None, "see #project/Tall-Tree-Group", names) == "Tall Tree Group"
    assert library_index.project_for_note("Misc", ["work"], "#project/Unknown", names) is None

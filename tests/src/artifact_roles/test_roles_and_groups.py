"""The one classifier behind the Library and the vault tree.

Names come from the real vault: Tall Tree Group agreements with backups and
review packets, and Dono Fuel files with signed copies and receipts.
"""
import pytest

from src.artifact_roles import classify, group_files, normalize_stem

TT = "AI Mind/Projects/Tall Tree Group/Agreements"
DA = "Tall Tree Group — Deliverable Agreement 01 — Initial Server Setup"


@pytest.mark.parametrize("path,siblings,role", [
    (f"{TT}/{DA}.docx", [f"{DA}.docx"], "master"),
    (f"{TT}/{DA}.pdf", [f"{DA}.pdf"], "export"),
    (f"{TT}/{DA}.md", [], "master"),
    (f"{TT}/{DA}.docx", [f"{DA}.md", f"{DA}.docx"], "export"),
    (f"{TT}/{DA} v2.docx", [f"{DA}.md", f"{DA} v2.docx"], "export"),
    (f"{TT}/{DA}.docx", [f"Other.md"], "master"),
    ("Dono Fuel/Dono Fuel Agreement - SIGNED.pdf", [], "signed"),
    ("Dono Fuel/Agreement_executed.docx", [], "signed"),
    ("Dono Fuel/Countersigned NDA.pdf", [], "signed"),
    ("Dono Fuel/Unsigned draft.docx", [], "master"),
    ("Dono Fuel/Designed layout.pdf", [], "export"),
    (f"{TT}/Backups/20261021-215722-contact-defaults/{DA}.docx", [], "history"),
    (f"{TT}/Review Packets/remaining-pdf-migration-20261022T233727Z/{DA}.pdf", [], "history"),
    (f"{TT}/backups/{DA}-signed.pdf", [], "history"),
    (f"{TT}/{DA} (1).docx", [], "history"),
    (f"{TT}/{DA}.docx.bak", [], "history"),
    (f"{TT}/{DA}.docx~", [], "history"),
    (f"{TT}/Archive/old.md", [], "history"),
    (f"{TT}/{DA}.preview.html", [], "supporting"),
    (f"{TT}/{DA}.pdf.sha256", [], "supporting"),
    (f"{TT}/receipt.json", [], "supporting"),
    (f"{TT}/Receipts/payment.pdf", [], "supporting"),
    (f"{TT}/Previews/page1.png", [], "supporting"),
    (f"{TT}/site photo.JPG", [], "source"),
    (f"{TT}/Originals/scan.pdf", [], "source"),
    (f"{TT}/budget.xlsx", [], None),
])
def test_role(path, siblings, role):
    assert classify(path, siblings=siblings) == role


@pytest.mark.parametrize("name,stem", [
    ("Agreement v2.docx", "agreement"),
    ("Agreement-v3.pdf", "agreement"),
    ("Agreement (1).docx", "agreement"),
    ("Agreement copy.docx", "agreement"),
    ("Agreement_signed.pdf", "agreement"),
    ("Agreement - Executed.pdf", "agreement"),
    ("Agreement-20261021.pdf", "agreement"),
    ("Agreement 2026-10-21.pdf", "agreement"),
    ("Agreement-20261021T233727Z.pdf", "agreement"),
    ("Agreement 20261021-215722.docx", "agreement"),
    ("Agreement v2 (1) signed.pdf", "agreement"),
    ("Deliverable Agreement 01.docx", "deliverable agreement 01"),
    ("  Mixed   CASE.MD", "mixed case"),
    ("20261021.md", "20261021"),
    ("notes v1.2", "notes v1.2"),
])
def test_normalize_stem(name, stem):
    assert normalize_stem(name) == stem


def _f(path, mtime=100):
    return {"path": path, "mtime": mtime}


def _by_title(groups):
    return {g["title"]: g for g in groups}


def test_docx_with_pdf_export_and_history_makes_one_group():
    groups = group_files([
        _f(f"{TT}/{DA}.docx", 300),
        _f(f"{TT}/{DA}.pdf", 310),
        _f(f"{TT}/{DA}.preview.html", 310),
        _f(f"{TT}/Backups/20261021-215722-contact-defaults/{DA}.docx", 100),
        _f(f"{TT}/Backups/20261020-100000-first/{DA}.docx", 50),
        _f(f"{TT}/Review Packets/remaining-pdf-migration-20261022T233727Z/{DA}.pdf", 200),
    ])
    assert len(groups) == 1
    g = groups[0]
    assert g["title"] == DA
    assert g["master"]["path"] == f"{TT}/{DA}.docx"
    assert g["current_export"]["path"].endswith(".pdf")
    assert [h["mtime"] for h in g["history"]] == [200, 100, 50]
    assert [s["name"] for s in g["supporting"]] == [f"{DA}.preview.html"]
    assert g["warnings"] == []


def test_markdown_master_makes_docx_an_export_and_warns_when_word_is_newer():
    groups = group_files([
        _f("Dono Fuel/Quote.md", 100),
        _f("Dono Fuel/Quote.docx", 200),
        _f("Dono Fuel/Quote.pdf", 150),
    ])
    (g,) = groups
    assert g["master"]["name"] == "Quote.md"
    assert g["current_export"]["name"] == "Quote.docx"  # newest export
    assert [h["name"] for h in g["history"]] == ["Quote.pdf"]
    assert g["warnings"] == ["word_newer_than_master"]


def test_no_warning_when_markdown_is_newer():
    (g,) = group_files([_f("A/Quote.md", 300), _f("A/Quote.docx", 200)])
    assert g["warnings"] == []


def test_signed_copy_is_listed_on_the_group_and_not_the_master():
    (g,) = group_files([
        _f("Dono Fuel/Agreement.docx", 100),
        _f("Dono Fuel/Agreement_signed.pdf", 200),
    ])
    assert g["master"]["name"] == "Agreement.docx"
    assert [s["name"] for s in g["signed"]] == ["Agreement_signed.pdf"]
    assert g["current_export"] is None


def test_copy_suffix_is_history_of_the_original():
    (g,) = group_files([_f("A/Plan.docx", 200), _f("A/Plan (1).docx", 300)])
    assert g["master"]["name"] == "Plan.docx"
    assert [h["name"] for h in g["history"]] == ["Plan (1).docx"]


def test_lone_copy_still_shows_up_as_itself():
    (g,) = group_files([_f("A/Plan (1).docx", 300)])
    assert g["master"]["name"] == "Plan (1).docx"


def test_older_version_of_same_stem_becomes_history():
    (g,) = group_files([_f("A/Plan v2.docx", 300), _f("A/Plan.docx", 100)])
    assert g["master"]["name"] == "Plan v2.docx"
    assert [h["name"] for h in g["history"]] == ["Plan.docx"]


def test_groups_do_not_cross_folders():
    groups = group_files([_f("A/Plan.docx"), _f("B/Plan.docx")])
    assert sorted(g["folder"] for g in groups) == ["A", "B"]


def test_history_attaches_to_nearest_non_history_ancestor_group():
    groups = group_files([
        _f("P/Agreements/Plan.docx", 300),
        _f("P/Plan.docx", 100),
        _f("P/Agreements/Backups/2026/Plan.docx", 50),
    ])
    inner = next(g for g in groups if g["folder"] == "P/Agreements")
    outer = next(g for g in groups if g["folder"] == "P")
    assert [h["mtime"] for h in inner["history"]] == [50]
    assert outer["history"] == []


def test_review_packet_attaches_to_unique_group_below_the_packet_parent():
    groups = group_files([
        _f("P/Agreements/Plan.docx", 300),
        _f("P/Review Packets/pkt-1/Plan.docx", 50),
        _f("P/Review Packets/pkt-1/Unrelated.docx", 50),
    ])
    assert len(groups) == 1
    assert [h["path"] for h in groups[0]["history"]] == ["P/Review Packets/pkt-1/Plan.docx"]


def test_history_without_a_group_is_dropped_not_promoted():
    assert group_files([_f("P/Backups/Only.docx"), _f("P/Receipts/r.pdf")]) == []


def test_loose_supporting_files_attach_to_the_only_group_in_the_folder():
    (g,) = group_files([_f("A/Plan.docx", 1), _f("A/render.html", 2)])
    assert [s["name"] for s in g["supporting"]] == ["render.html"]


def test_images_beside_a_master_are_supporting_but_stand_alone_otherwise():
    groups = group_files([_f("A/Plan.docx"), _f("A/Plan.png"), _f("A/site.jpg")])
    by_title = _by_title(groups)
    assert [s["name"] for s in by_title["Plan"]["supporting"]] == ["Plan.png"]
    assert by_title["site"]["primary"]["role"] == "source"


def test_backslash_and_empty_paths_are_tolerated():
    groups = group_files([_f(r"A\Plan.docx"), _f("")])
    assert [g["folder"] for g in groups] == ["A"]

"""Filesystem containment for client-supplied import paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from lotus_mcp.models import SourceType
from lotus_mcp.security import (
    PathSecurityError,
    csv_safe_cell,
    resolve_within_root,
    sanitize_filename,
)
from lotus_mcp.services.import_service import ImportRefused

from .conftest import csv_document, csv_row

TRAVERSAL_PATHS = [
    "../outside.csv",
    "../../etc/passwd",
    "subdir/../../outside.csv",
    "..\\outside.csv",
    "subdir\\..\\..\\outside.csv",
    "/etc/passwd",
    "C:\\Windows\\win.ini",
    "C:/Windows/win.ini",
    "\\\\server\\share\\file.csv",
    "//server/share/file.csv",
]


@pytest.mark.parametrize("candidate", TRAVERSAL_PATHS)
def test_escape_attempts_are_rejected(tmp_path: Path, candidate: str):
    root = tmp_path / "incoming"
    root.mkdir()
    with pytest.raises(PathSecurityError):
        resolve_within_root(root, candidate)


@pytest.mark.parametrize("candidate", ["", "   ", "with\x00nul"])
def test_degenerate_paths_are_rejected(tmp_path: Path, candidate: str):
    with pytest.raises(PathSecurityError):
        resolve_within_root(tmp_path, candidate)


def test_ordinary_relative_paths_resolve(tmp_path: Path):
    root = tmp_path / "incoming"
    (root / "nested").mkdir(parents=True)
    assert resolve_within_root(root, "file.csv") == (root / "file.csv").resolve()
    assert resolve_within_root(root, "nested/file.csv") == (root / "nested/file.csv").resolve()
    assert resolve_within_root(root, "./file.csv") == (root / "file.csv").resolve()


def test_import_refuses_traversal_before_touching_the_database(ctx, tmp_path: Path):
    outside = tmp_path.parent / "outside.csv"
    outside.write_text(csv_document(csv_row()), encoding="utf-8")
    with pytest.raises(ImportRefused):
        ctx.imports.import_file("../outside.csv", source_type=SourceType.MANUAL_CSV)
    assert ctx.database.counts()["batches"] == 0


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    """Create a symlink, skipping the test only where the OS forbids it.

    Windows allows this under Developer Mode or with elevation; probing at
    runtime keeps the escape defence genuinely exercised wherever it can be,
    instead of being skipped by platform name.
    """
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("this environment does not permit creating symlinks")


def test_resolve_rejects_a_symlink_leaving_the_root(tmp_path: Path):
    root = tmp_path / "incoming"
    root.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("x", encoding="utf-8")
    _symlink(root / "link.csv", outside)

    with pytest.raises(PathSecurityError, match="escapes the approved import root"):
        resolve_within_root(root, "link.csv")


def test_resolve_allows_a_symlink_that_stays_inside_the_root(tmp_path: Path):
    root = tmp_path / "incoming"
    (root / "nested").mkdir(parents=True)
    real = root / "nested" / "real.csv"
    real.write_text("x", encoding="utf-8")
    _symlink(root / "link.csv", real)

    # Containment, not link-phobia: a link that resolves inside the root is fine.
    assert resolve_within_root(root, "link.csv") == real.resolve()


def test_symlink_pointing_outside_the_root_is_rejected(ctx, workspace: Path, incoming: Path):
    secret = workspace / "outside" / "secret.csv"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(csv_document(csv_row()), encoding="utf-8")
    _symlink(incoming / "link.csv", secret)

    # The link sits inside the root, but containment is checked after the link
    # is resolved — so the escape is caught.
    with pytest.raises(ImportRefused, match="escapes the approved import root"):
        ctx.imports.import_file("link.csv", source_type=SourceType.MANUAL_CSV)


def test_symlinked_directory_escape_is_rejected(ctx, workspace: Path, incoming: Path):
    outside_dir = workspace / "outside_dir"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "secret.csv").write_text(csv_document(csv_row()), encoding="utf-8")
    _symlink(incoming / "hop", outside_dir, directory=True)

    with pytest.raises(ImportRefused, match="escapes the approved import root"):
        ctx.imports.import_file("hop/secret.csv", source_type=SourceType.MANUAL_CSV)


def test_directory_is_not_importable(ctx, incoming: Path):
    (incoming / "a_folder.csv").mkdir()
    with pytest.raises(ImportRefused, match="regular files"):
        ctx.imports.import_file("a_folder.csv", source_type=SourceType.MANUAL_CSV)


def test_missing_file_message_does_not_leak_the_host_path(ctx):
    with pytest.raises(ImportRefused) as excinfo:
        ctx.imports.import_file("nope.csv", source_type=SourceType.MANUAL_CSV)
    message = str(excinfo.value)
    assert "nope.csv" in message
    assert str(ctx.config.paths.import_root) not in message


def test_sanitize_filename_strips_directories_and_control_characters():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("C:\\Users\\me\\moods.csv") == "moods.csv"
    assert sanitize_filename("bad\nname.csv") == "bad_name.csv"
    assert sanitize_filename("") == "unnamed"
    assert sanitize_filename("CON.csv").startswith("_CON")


def test_sanitize_filename_bounds_length():
    assert len(sanitize_filename("a" * 500 + ".csv")) <= 120


def test_csv_safe_cell_disarms_formula_prefixes():
    for dangerous in ("=cmd", "+1", "-1", "@SUM(A1)"):
        assert csv_safe_cell(dangerous).startswith("'")
    assert csv_safe_cell("overwhelmed") == "overwhelmed"

"""Shared test fixtures.

Every test runs against a throwaway workspace under ``tmp_path`` — its own
import root, its own SQLite file. Nothing here touches a real database or real
mood data, and all sample content is synthetic.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from lotus_mcp.config import AppConfig
from lotus_mcp.server import ServerContext

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    for name in ("imports/incoming", "imports/processed", "imports/failed", "data"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def make_context(workspace: Path) -> Callable[..., ServerContext]:
    """Build a ServerContext with optional privacy/limit overrides.

    Defaults mirror the shipped configuration, so a test that overrides nothing
    is exercising exactly what an operator gets out of the box.
    """

    def factory(**overrides: Any) -> ServerContext:
        privacy = overrides.pop("privacy", {})
        limits = overrides.pop("limits", {})
        config = AppConfig.model_validate(
            {
                "paths": {
                    "import_root": str(workspace / "imports/incoming"),
                    "processed_dir": str(workspace / "imports/processed"),
                    "failed_dir": str(workspace / "imports/failed"),
                    "database_path": str(workspace / "data/mood.db"),
                    "state_dir": str(workspace / "data"),
                },
                "privacy": privacy,
                "limits": limits,
                **overrides,
            }
        )
        return ServerContext(config)

    return factory


@pytest.fixture
def ctx(make_context: Callable[..., ServerContext]) -> ServerContext:
    """A context with the shipped, conservative defaults."""
    return make_context()


@pytest.fixture
def incoming(workspace: Path) -> Path:
    return workspace / "imports/incoming"


@pytest.fixture
def drop(incoming: Path) -> Callable[[str, str], Path]:
    """Write a file into the import root and return its path."""

    def _drop(name: str, content: str) -> Path:
        path = incoming / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    return _drop


@pytest.fixture
def copy_fixture(incoming: Path) -> Callable[[str], Path]:
    def _copy(name: str) -> Path:
        target = incoming / name
        target.write_text((FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
        return target

    return _copy


CSV_HEADER = "id,timestamp,emotion,pleasantness,energy,intensity,tags,note\n"


def csv_row(
    record_id: str = "r1",
    timestamp: str = "2026-08-03T09:15:00-04:00",
    emotion: str = "overwhelmed",
    valence: str = "-0.6",
    energy: str = "0.7",
    intensity: str = "0.8",
    tags: str = "work",
    note: str = "",
) -> str:
    note_cell = f'"{note}"' if ("," in note or "\n" in note or '"' in note) else note
    return f"{record_id},{timestamp},{emotion},{valence},{energy},{intensity},{tags},{note_cell}\n"


def csv_document(*rows: str) -> str:
    return CSV_HEADER + "".join(rows or (csv_row(),))

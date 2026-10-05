"""Odysseus test suite. A package, so tests/routes/, tests/src/ and tests/core/
mirror the production tree without shadowing the production packages."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

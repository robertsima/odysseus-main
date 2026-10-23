"""A chat saves what its open document stands for: itself, the project, or nothing."""
import pytest

from src import session_settings


@pytest.mark.parametrize("target", ["document", "project", "general"])
def test_the_three_targets_are_accepted(target):
    assert session_settings.validate_patch({"doc_target": target}) == {"doc_target": target}


def test_null_clears_the_saved_target():
    assert session_settings.validate_patch({"doc_target": None}) == {"doc_target": None}


@pytest.mark.parametrize("bad", ["", "all", "Document", 3, True, ["document"]])
def test_anything_else_is_refused(bad):
    with pytest.raises(ValueError, match="doc_target"):
        session_settings.validate_patch({"doc_target": bad})

"""DELETE /api/personal/remove_directory acts only inside the personal docs folder.

The path comes from the query string and goes to the index's removal code,
so a ``..`` path or one elsewhere on disk must be refused before it gets there.
"""
import os

import pytest

from routes import personal_routes

pytestmark = pytest.mark.security


@pytest.fixture
def removals(api, monkeypatch):
    removed = []
    monkeypatch.setattr(api.module.personal_docs_mgr, "remove_directory", removed.append, raising=False)
    return removed


def _remove(api, directory):
    return api.as_admin().delete("/api/personal/remove_directory", params={"directory": directory})


@pytest.mark.parametrize("directory", ["../outside", "{tmp}"], ids=["dot-dot", "absolute"])
def test_a_directory_outside_the_personal_folder_is_refused(api, removals, tmp_path, directory):
    response = _remove(api, directory.format(tmp=tmp_path))

    assert response.status_code == 403
    assert removals == []


def test_a_directory_inside_the_personal_folder_is_removed(api, removals):
    notes = os.path.join(personal_routes.PERSONAL_DIR, "notes")
    os.makedirs(notes, exist_ok=True)

    response = _remove(api, "notes")

    assert response.status_code == 200, response.text
    assert removals == [os.path.realpath(notes)]

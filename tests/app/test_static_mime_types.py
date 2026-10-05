"""The app serves its ES modules as JavaScript whatever the OS maps .js to.

Some Windows installs map .js to text/plain in the registry, and Python's
mimetypes reads that map. A module script served as text/plain is refused by
the browser, so the page loads but nothing on it works.
"""
import mimetypes


def test_js_modules_are_served_as_javascript_when_the_os_maps_js_to_text(api, monkeypatch):
    monkeypatch.setitem(mimetypes.types_map, ".js", "text/plain")
    monkeypatch.setitem(mimetypes.types_map, ".mjs", "text/plain")

    api.module.register_static_mime_types()

    response = api.as_user("alice").get("/static/js/ui.js")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "text/javascript"
    assert mimetypes.guess_type("module.mjs")[0] == "application/javascript"

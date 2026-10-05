"""GET /api/cookbook/packages for a package installed while the server runs.

The Cookbook installs a dependency with `pip install --user` into a
long-lived server process. The user site-packages directory may not have
existed when the process started, so Python never put it on the import path.
The probe has to look there, or the Dependencies panel keeps showing the
package as missing until the server restarts.
"""
import sys

from routes import shell_routes  # noqa: F401  (registers the route module)


def test_a_package_installed_into_the_user_site_after_startup_is_found(api, monkeypatch, tmp_path):
    import site

    user_site = tmp_path / "user-site"
    (user_site / "hf_transfer").mkdir(parents=True)
    (user_site / "hf_transfer" / "__init__.py").write_text("")
    dist_info = user_site / "hf_transfer-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: hf_transfer\nVersion: 1.0\n")
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(user_site))
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(user_site)])
    monkeypatch.delitem(sys.modules, "hf_transfer", raising=False)

    response = api.as_admin().get("/api/cookbook/packages")

    assert response.status_code == 200, response.text
    rows = {p["name"]: p for p in response.json()["packages"]}
    assert rows["hf_transfer"]["installed"] is True
    sys.modules.pop("hf_transfer", None)

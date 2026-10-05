"""The Cookbook packages panel reports what is installed, under the right distribution name.

llama-cpp-python was shown as "not installed" while it was: the check looked up
metadata under the import name munged to "llama-cpp", but the distribution is
llama-cpp-python (issue #1020). The panel also has to list transformers, which
the image pipelines need.
"""
import importlib
import importlib.metadata

import pytest

from routes import shell_routes


@pytest.fixture
def packages(api, monkeypatch):
    def list_packages(installed_distributions, **query):
        def version(name):
            if name in installed_distributions:
                return "1.0"
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", version)
        monkeypatch.setattr(shell_routes, "_import_optional_dependency_for_status", lambda name: None)
        monkeypatch.setattr(shell_routes.shutil, "which", lambda name, *args, **kwargs: None)
        response = api.as_admin().get("/api/cookbook/packages", params=query)
        assert response.status_code == 200, response.text
        return {pkg["name"]: pkg for pkg in response.json()["packages"]}

    return list_packages


def test_a_package_is_found_under_its_distribution_name_not_its_import_name(packages):
    assert packages({"llama-cpp-python"})["llama_cpp"]["installed"] is True
    assert packages({"llama-cpp"})["llama_cpp"]["installed"] is False


def test_transformers_is_listed_for_a_krea_model_and_installable(api, packages, monkeypatch):
    transformers = packages(set(), model_hint="FLUX.1-Krea-dev")["transformers"]
    assert transformers["category"] == "Image"
    assert transformers["pip"] == "transformers"
    assert "transformers" not in packages(set(), model_hint="llama-3")

    commands = []

    async def fake_pip(*cmd, **kwargs):
        commands.append(cmd)

        class Proc:
            returncode = 0

            async def communicate(self):
                return b"installed", b""

        return Proc()

    monkeypatch.setattr(shell_routes.asyncio, "create_subprocess_exec", fake_pip)
    response = api.as_admin().post("/api/cookbook/packages/install", json={"pip": transformers["pip"]})

    assert response.json()["ok"] is True
    assert commands[0][-2:] == ("install", "transformers")

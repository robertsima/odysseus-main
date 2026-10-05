"""Centralized upload byte-limits (issue #3364).

Every per-route upload limit lives in ``src.upload_limits`` as a module-level
constant read through the validated ``read_byte_limit_env``. These tests pin:
- the default values (unchanged from the prior per-route literals),
- env-overridability for each one,
- that an invalid env value fails fast (validation), and
- that the routes import the constant from upload_limits rather than redefining
  it locally (no scattered raw getenv / hardcoded literal).
"""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# const name -> (env var, default bytes)
_LIMITS = {
    "GALLERY_UPLOAD_MAX_BYTES": ("ODYSSEUS_GALLERY_UPLOAD_MAX_BYTES", 100 * 1024 * 1024),
    "GALLERY_TRANSFORM_UPLOAD_MAX_BYTES": ("ODYSSEUS_GALLERY_TRANSFORM_UPLOAD_MAX_BYTES", 25 * 1024 * 1024),
    "MEMORY_IMPORT_MAX_BYTES": ("ODYSSEUS_MEMORY_IMPORT_MAX_BYTES", 10 * 1024 * 1024),
    "PERSONAL_UPLOAD_MAX_BYTES": ("ODYSSEUS_PERSONAL_UPLOAD_MAX_BYTES", 25 * 1024 * 1024),
    "EMAIL_COMPOSE_UPLOAD_MAX_BYTES": ("ODYSSEUS_EMAIL_COMPOSE_UPLOAD_MAX_BYTES", 25 * 1024 * 1024),
    "STT_MAX_AUDIO_BYTES": ("ODYSSEUS_STT_MAX_AUDIO_BYTES", 25 * 1024 * 1024),
    "ICS_MAX_BYTES": ("ODYSSEUS_ICS_MAX_BYTES", 10 * 1024 * 1024),
}


def _fresh_upload_limits():
    """Run src/upload_limits.py again as a separate module object.

    The limits are read at import. Reloading the real module would rebind
    them under the routes that imported them, so the copy stays out of
    sys.modules.
    """
    spec = importlib.util.find_spec("src.upload_limits")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reload_clean(monkeypatch):
    """A fresh upload_limits with all the limit env vars unset."""
    for env, _ in _LIMITS.values():
        monkeypatch.delenv(env, raising=False)
    return _fresh_upload_limits()


@pytest.mark.parametrize("name,env,default", [(n, e, d) for n, (e, d) in _LIMITS.items()])
def test_default_value(monkeypatch, name, env, default):
    mod = _reload_clean(monkeypatch)
    assert getattr(mod, name) == default


@pytest.mark.parametrize("name,env,default", [(n, e, d) for n, (e, d) in _LIMITS.items()])
def test_env_override(monkeypatch, name, env, default):
    for e, _ in _LIMITS.values():
        monkeypatch.delenv(e, raising=False)
    monkeypatch.setenv(env, "4242")
    mod = _fresh_upload_limits()
    assert getattr(mod, name) == 4242


@pytest.mark.parametrize("env", [e for e, _ in _LIMITS.values()])
def test_invalid_env_fails_fast(monkeypatch, env):
    for e, _ in _LIMITS.values():
        monkeypatch.delenv(e, raising=False)
    monkeypatch.setenv(env, "not-an-int")
    with pytest.raises(ValueError, match=env):
        _fresh_upload_limits()


@pytest.mark.parametrize("env", [e for e, _ in _LIMITS.values()])
def test_non_positive_env_rejected(monkeypatch, env):
    for e, _ in _LIMITS.values():
        monkeypatch.delenv(e, raising=False)
    monkeypatch.setenv(env, "0")
    with pytest.raises(ValueError, match="greater than 0"):
        _fresh_upload_limits()


# route module -> (attribute the route enforces, limit it must follow)
_ROUTE_LIMITS = {
    "routes.gallery.gallery_routes": "GALLERY_UPLOAD_MAX_BYTES",
    "routes.memory.memory_routes": "MEMORY_IMPORT_MAX_BYTES",
    "routes.personal_routes": "PERSONAL_UPLOAD_MAX_BYTES",
    "routes.email_routes": "EMAIL_COMPOSE_UPLOAD_MAX_BYTES",
    "routes.stt_routes": "STT_MAX_AUDIO_BYTES",
    "routes.calendar_routes": "ICS_MAX_BYTES",
}


def test_routes_enforce_the_configured_limit_not_a_local_copy(tmp_path):
    """A route with its own literal ignores the operator's env override."""
    import json
    import os
    import subprocess
    import sys

    overrides = {env: str(1000 + i) for i, (env, _) in enumerate(_LIMITS.values())}
    code = (
        "import importlib, json;"
        f"routes = {list(_ROUTE_LIMITS.items())!r};"
        "print(json.dumps({m: getattr(importlib.import_module(m), a) for m, a in routes}))"
    )
    env = {**os.environ, **overrides, "PYTHONPATH": str(REPO),
           "DATABASE_URL": "sqlite:///" + (tmp_path / "app.db").as_posix(),
           "ODYSSEUS_DATA_DIR": str(tmp_path / "data"), "PYTHONUTF8": "1"}
    result = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env,
                            capture_output=True, text=True, timeout=180)

    assert result.returncode == 0, result.stderr[-2000:]
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    expected = {module: int(overrides[_LIMITS[attr][0]]) for module, attr in _ROUTE_LIMITS.items()}
    assert seen == expected

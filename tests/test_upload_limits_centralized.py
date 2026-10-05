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


def test_routes_import_from_upload_limits_not_local_defs():
    """Routes must import the constant, not redefine it via raw getenv / literal."""
    forbidden = {
        "routes/gallery/gallery_routes.py": [
            'int(os.getenv("ODYSSEUS_GALLERY_UPLOAD_MAX_BYTES"',
            'int(os.getenv("ODYSSEUS_GALLERY_TRANSFORM_UPLOAD_MAX_BYTES"',
        ],
        "routes/memory/memory_routes.py": ['int(os.getenv("ODYSSEUS_MEMORY_IMPORT_MAX_BYTES"'],
        "routes/personal_routes.py": ['os.getenv("ODYSSEUS_PERSONAL_UPLOAD_MAX_BYTES"'],
        "routes/email_routes.py": ["EMAIL_COMPOSE_UPLOAD_MAX_BYTES = 25 * 1024 * 1024"],
        "routes/stt_routes.py": ["STT_MAX_AUDIO_BYTES = 25 * 1024 * 1024"],
        "routes/calendar_routes.py": ["_ICS_MAX_BYTES = 10 * 1024 * 1024"],
    }
    for path, needles in forbidden.items():
        text = (REPO / path).read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, f"{path} still defines limit locally: {needle}"

    # And each imports from upload_limits.
    imports = {
        "routes/gallery/gallery_routes.py": "GALLERY_UPLOAD_MAX_BYTES",
        "routes/memory/memory_routes.py": "MEMORY_IMPORT_MAX_BYTES",
        "routes/personal_routes.py": "PERSONAL_UPLOAD_MAX_BYTES",
        "routes/email_routes.py": "EMAIL_COMPOSE_UPLOAD_MAX_BYTES",
        "routes/stt_routes.py": "STT_MAX_AUDIO_BYTES",
        "routes/calendar_routes.py": "ICS_MAX_BYTES",
    }
    for path, const in imports.items():
        text = (REPO / path).read_text(encoding="utf-8")
        assert "from src.upload_limits import" in text
        assert const in text

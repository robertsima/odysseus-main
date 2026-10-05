"""Startup registers every integration's skills before it seeds the bundled ones.

Seeding installs the skills registered so far. An integration registered after
the seed pass is missing from a fresh deployment until the next restart.

initialize_managers builds process-wide singletons, so it runs in a child
process with its own scratch data folder.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

SCRIPT = textwrap.dedent("""
    import os
    import src.builtin_skills
    from src import app_initializer, integration_registry
    events = []
    src.builtin_skills.register_integration_skills = lambda name, dirs: events.append("register")
    app_initializer.seed_bundled_skills = lambda manager: events.append("seed")
    app_initializer.initialize_managers(os.getcwd(), None)
    print("EVENTS", ",".join(events))
    print("INTEGRATIONS", len(integration_registry.all()))
""")


def test_every_integration_is_registered_before_the_bundled_seed(tmp_path):
    env = {
        **os.environ,
        "ODYSSEUS_DATA_DIR": str(tmp_path / "data"),
        "DATABASE_URL": "sqlite:///" + (tmp_path / "app.db").as_posix(),
        "PYTHON_DOTENV_DISABLED": "1",
        "ODYSSEUS_DISABLE_MCP": "1",
        "ODYSSEUS_FILE_LOG": "0",
        "CHROMADB_HOST": "127.0.0.1",
        "CHROMADB_PORT": "9",
        "CHROMADB_CONNECT_TIMEOUT": "0.01",
        "PYTHONUTF8": "1",
    }

    result = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=180)

    lines = dict(line.split(" ", 1) for line in result.stdout.splitlines() if line.startswith(("EVENTS", "INTEGRATIONS")))
    assert "EVENTS" in lines, result.stderr[-2000:]
    events = lines["EVENTS"].split(",")
    assert int(lines["INTEGRATIONS"]) > 0
    assert events == ["register"] * int(lines["INTEGRATIONS"]) + ["seed"]

"""Startup re-indexes the vault when the vector store came back empty.

Indexing is one-shot and tracked in JSON, so after the vector store is reset
the app believes the vault is indexed while every chat turn retrieves nothing.
initialize_managers must run the emptiness check.

initialize_managers builds the process-wide session manager and upload
handler, so it runs in a child process with its own scratch data folder.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

SCRIPT = textwrap.dedent("""
    import os
    from src import app_initializer
    calls = []
    app_initializer.PersonalDocsManager.reindex_if_empty = lambda self: calls.append(self)
    app_initializer.initialize_managers(os.getcwd(), None)
    print("REINDEX_CALLS", len(calls))
""")


def test_startup_checks_for_an_emptied_vector_store(tmp_path):
    env = {
        **os.environ,
        "ODYSSEUS_DATA_DIR": str(tmp_path / "data"),
        "DATABASE_URL": "sqlite:///" + (tmp_path / "app.db").as_posix(),
        "PYTHON_DOTENV_DISABLED": "1",
        "ODYSSEUS_DISABLE_MCP": "1",
        "ODYSSEUS_FILE_LOG": "0",
        # Nothing listens on port 9, so the vector stores fail fast.
        "CHROMADB_HOST": "127.0.0.1",
        "CHROMADB_PORT": "9",
        "CHROMADB_CONNECT_TIMEOUT": "0.01",
        "PYTHONUTF8": "1",
    }

    result = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=180)

    assert "REINDEX_CALLS 1" in result.stdout, result.stderr[-2000:]

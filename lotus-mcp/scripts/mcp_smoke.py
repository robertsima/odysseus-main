"""End-to-end MCP smoke test.

Spawns the real server as a subprocess over stdio, completes the MCP
handshake, lists the tools, and exercises the three behaviours that matter
most: a dry run mutates nothing, aggregates work, and raw-entry access is
refused by the default policy.

    python scripts/mcp_smoke.py

Exits 0 on success. Uses a throwaway workspace and synthetic data only.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent

SAMPLE_CSV = """id,timestamp,emotion,pleasantness,energy,intensity,tags,note
smoke-1,2026-08-03T09:00:00-04:00,drained,-0.5,0.1,0.6,work,synthetic note
smoke-2,2026-08-03T14:00:00-04:00,content,0.4,0.5,0.3,lunch,
smoke-3,2026-08-04T08:30:00-04:00,flat,-0.2,0.2,0.4,work,
"""

CONFIG = """
privacy:
  expose_raw_entries: false
  expose_notes: false
paths:
  import_root: {root}/imports/incoming
  processed_dir: {root}/imports/processed
  failed_dir: {root}/imports/failed
  database_path: {root}/data/mood.db
  state_dir: {root}/data
"""


def _payload(result) -> dict:
    """MCP tool results come back as text content; ours is always JSON."""
    return json.loads(result.content[0].text)


def _check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        raise SystemExit(f"smoke test failed: {label}")


async def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="lotus-smoke-"))
    try:
        for sub in ("imports/incoming", "imports/processed", "imports/failed", "data"):
            (workspace / sub).mkdir(parents=True, exist_ok=True)
        (workspace / "imports/incoming/smoke.csv").write_text(SAMPLE_CSV, encoding="utf-8")
        config_path = workspace / "config.yaml"
        config_path.write_text(CONFIG.format(root=workspace.as_posix()), encoding="utf-8")

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "lotus_mcp.server"],
            env={
                "LOTUS_CONFIG": str(config_path),
                "PYTHONPATH": str(REPO / "src"),
                "PYTHONIOENCODING": "utf-8",
            },
        )

        print("MCP stdio smoke test")
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                print(f"  connected to '{init.serverInfo.name}' v{init.serverInfo.version}")

                names = [tool.name for tool in (await session.list_tools()).tools]
                _check("all five tools advertised", len(names) == 5, ", ".join(names))

                dry = _payload(
                    await session.call_tool(
                        "mood.import_file",
                        {"path": "smoke.csv", "source_type": "manual_csv", "dry_run": True},
                    )
                )
                _check("dry run reports 3 records", dry.get("record_count") == 3)
                _check("dry run moved nothing", dry.get("file_disposition") == "unchanged")
                _check(
                    "dry run wrote nothing",
                    (workspace / "imports/incoming/smoke.csv").exists()
                    and not any((workspace / "imports/processed").iterdir()),
                )

                committed = _payload(
                    await session.call_tool(
                        "mood.import_file",
                        {"path": "smoke.csv", "source_type": "manual_csv", "dry_run": False},
                    )
                )
                _check("import committed", committed.get("status") == "success")
                _check("3 entries inserted", committed.get("inserted_count") == 3)
                _check("notes not imported", committed.get("notes_imported") is False)
                _check(
                    "file moved to processed",
                    (workspace / "imports/processed/smoke.csv").exists(),
                )

                status = _payload(await session.call_tool("mood.get_import_status", {"limit": 5}))
                batch = status["batches"][0]
                _check("status shows a basename only", batch["filename"] == "smoke.csv")
                _check(
                    "status leaks no host path",
                    str(workspace) not in json.dumps(status),
                )
                _check("status leaks no note text", "synthetic note" not in json.dumps(status))

                summary = _payload(
                    await session.call_tool(
                        "mood.summarize_period",
                        {
                            "start": "2026-08-01T00:00:00+00:00",
                            "end": "2026-08-10T00:00:00+00:00",
                            "group_by": "day",
                        },
                    )
                )
                _check("summary counts all 3 entries", summary.get("total_entries") == 3)
                _check("summary reports notes untouched", summary.get("notes_accessed") is False)
                _check("summary carries a non-diagnostic disclaimer", "disclaimer" in summary)

                patterns = _payload(
                    await session.call_tool(
                        "mood.detect_low_energy_patterns",
                        {
                            "lookback_days": 365,
                            "group_by": "day_of_week",
                            "confirm_extended_range": True,
                        },
                    )
                )
                _check("low-energy analysis reports sample size", "sample_size" in patterns)
                _check("low-energy analysis reports limitations", bool(patterns.get("limitations")))

                denied = _payload(
                    await session.call_tool(
                        "mood.search_entries",
                        {
                            "start": "2026-08-01T00:00:00+00:00",
                            "end": "2026-08-10T00:00:00+00:00",
                        },
                    )
                )
                _check("raw entries denied by default", denied.get("error") == "policy_denied")

                notes_denied = _payload(
                    await session.call_tool(
                        "mood.search_entries",
                        {
                            "start": "2026-08-01T00:00:00+00:00",
                            "end": "2026-08-10T00:00:00+00:00",
                            "include_notes": True,
                        },
                    )
                )
                _check(
                    "client cannot enable notes by asking",
                    notes_denied.get("error") == "policy_denied",
                )

                escape = _payload(
                    await session.call_tool(
                        "mood.import_file",
                        {"path": "../../etc/passwd", "source_type": "manual_csv"},
                    )
                )
                _check("path traversal refused", escape.get("error") == "import_refused")

        print("\nMCP smoke test passed.")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

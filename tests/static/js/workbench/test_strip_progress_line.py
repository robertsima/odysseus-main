"""The agent strip's progress line says where a worker is and when it is waiting.

"round 74 · read_file" did not say which file, and a worker waiting to resume
after a provider error looked exactly like a stuck one (2026-10-08).
"""
import json
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]


def _fmt_progress(calls: str) -> list:
    # workbench.js imports half the UI, so evaluate just the pure helpers.
    src = (_REPO / "static" / "js" / "workbench.js").read_text(encoding="utf-8")
    body = src[src.index("function fmtDur("):src.index("function progressHtml(")].replace("export function", "function")
    res = subprocess.run(["node", "-e", body + f"\nconsole.log(JSON.stringify([{calls}]));"],
                         capture_output=True, timeout=30, text=True, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_the_line_names_what_the_current_tool_works_on():
    (line,) = _fmt_progress("""
        fmtProgress({round: 12, current_tool: 'read_file', current_target: 'static/js/theme.js'},
                    {startedAt: 1000, now: 1030}),
    """)

    assert line == {"text": "round 12 · read_file static/js/theme.js", "warn": False}


def test_a_worker_waiting_to_resume_says_so():
    (line,) = _fmt_progress("""
        fmtProgress({round: 74, waiting: 'Upstream model error; resuming in 30s (1/2)'},
                    {startedAt: 1000, now: 1030}),
    """)

    assert line == {"text": "round 74 · Upstream model error; resuming in 30s (1/2)", "warn": True}

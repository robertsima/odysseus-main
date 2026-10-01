"""Record the digest of every SKILL.md body a release has shipped.

`seed_bundled_skills` upgrades an installed bundled skill only when its digest
is in skills/.bundled-history.json (an unedited older release). Run this after
changing any bundled SKILL.md, and commit the result. It walks git history for
each bundled skill (following renames and the flat/categorised layouts), and
only ever adds digests, never removes them.

    python scripts/update_bundled_skill_history.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import builtin_skills  # noqa: E402


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, check=True,
    ).stdout.decode("utf-8", errors="replace")


def _historical_texts(rel_path: str):
    revisions = _git("log", "--follow", "--format=%H", "--name-only", "--", rel_path).split("\n")
    # --name-only interleaves the path each commit knew the file by (renames).
    commit = None
    for line in revisions:
        line = line.strip()
        if not line:
            continue
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            commit = line
        elif commit:
            try:
                yield _git("show", f"{commit}:{line}")
            except subprocess.CalledProcessError:
                continue
            commit = None


def main() -> int:
    path = os.path.join(ROOT, *builtin_skills._HISTORY_PATH)
    history = builtin_skills.load_bundled_history(ROOT)
    for category, name, *_rest in builtin_skills._BUNDLED_SKILLS:
        digests = history.setdefault(name, [])
        before = len(digests)
        candidates = [f"skills/{category}/{name}/SKILL.md", f"skills/{name}/SKILL.md"]
        texts = []
        for rel in candidates:
            try:
                texts.extend(_historical_texts(rel))
            except subprocess.CalledProcessError:
                pass
        source = os.path.join(builtin_skills._bundled_source(ROOT, category, name), "SKILL.md")
        with open(source, encoding="utf-8") as handle:
            texts.append(handle.read())  # working tree, possibly uncommitted
        for text in texts:
            try:
                digest = builtin_skills.skill_digest(text)
            except Exception:
                continue
            if digest not in digests:
                digests.append(digest)
        print(f"{name}: {len(digests)} digests (+{len(digests) - before})")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(history, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

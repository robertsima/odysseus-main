"""Record the digest of every SKILL.md body a release has shipped.

`seed_bundled_skills` upgrades an installed bundled skill only when its digest
is in skills/.bundled-history.json (an unedited older release). Run this after
changing any app-shipped SKILL.md (core, curated or integration), and commit the
result. It walks git history for each shipped skill (following renames and the
flat/categorised layouts), and only ever adds digests, never removes them.

    python scripts/update_bundled_skill_history.py
    python scripts/update_bundled_skill_history.py --skills-dir integrations/penpot/skills
"""
from __future__ import annotations

import argparse
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


def _targets(extra_dirs: list[str]) -> list[tuple[str, list[str], str]]:
    """(name, candidate repo-relative SKILL.md paths, working-tree SKILL.md) for
    every app-shipped skill: core and curated from skills/catalog.json, the
    integration skills the seeder can locate, and any extra directories."""
    targets = []
    for entry in builtin_skills.load_catalog(ROOT):
        category, name = entry["category"], entry["name"]
        source = os.path.join(builtin_skills._bundled_source(ROOT, category, name), "SKILL.md")
        targets.append((name, [f"skills/{category}/{name}/SKILL.md", f"skills/{name}/SKILL.md"], source))
    dirs = list(extra_dirs)
    for spec in builtin_skills._integration_specs(ROOT):
        dirs.append(spec["source_dir"])
    seen = {t[0] for t in targets}
    for skill_dir in builtin_skills._expand_skill_dirs(dirs):
        name = os.path.basename(skill_dir.rstrip("/\\"))
        if name in seen:
            continue
        seen.add(name)
        rel = os.path.relpath(skill_dir, ROOT).replace(os.sep, "/")
        targets.append((name, [f"{rel}/SKILL.md"], os.path.join(skill_dir, "SKILL.md")))
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--skills-dir", action="append", default=[], metavar="DIR",
        help="extra skill directory (or a parent of skill directories), e.g. integrations/<id>/skills; repeatable",
    )
    args = parser.parse_args(argv)
    path = os.path.join(ROOT, *builtin_skills._HISTORY_PATH)
    history = builtin_skills.load_bundled_history(ROOT)
    for name, candidates, source in _targets(args.skills_dir):
        digests = history.setdefault(name, [])
        before = len(digests)
        texts = []
        for rel in candidates:
            try:
                texts.extend(_historical_texts(rel))
            except subprocess.CalledProcessError:
                pass
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

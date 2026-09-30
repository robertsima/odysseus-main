"""Which reinstall a change's dependency edits call for, per project folder.

On 2026-09-29 an agent PR bumped Expo in `mobile/package.json` and its
lockfile. The PR said nothing about it; the user pulled, ran Expo with the
old node_modules, and the web app broke rendering an SVG icon. A day of turns
went to diagnosing what one line in the PR would have prevented: "after
pulling, run `npm ci` in mobile/". ``request_publish`` puts that line in the
PR body and hands it to the agent to tell the user.
"""
from __future__ import annotations

import posixpath
from typing import Dict, Iterable, List

# Lockfiles, not package.json alone: a package.json change is as often a
# script edit, and a real dependency change updates the lockfile too.
_REINSTALL = {
    "package-lock.json": ("npm", "npm ci"),
    "npm-shrinkwrap.json": ("npm", "npm ci"),
    "pnpm-lock.yaml": ("pnpm", "pnpm install --frozen-lockfile"),
    "yarn.lock": ("yarn", "yarn install --frozen-lockfile"),
    "bun.lockb": ("bun", "bun install --frozen-lockfile"),
    "pom.xml": ("maven", "./mvnw -U clean install (or reload the Maven project)"),
    "build.gradle": ("gradle", "./gradlew --refresh-dependencies build (or sync the Gradle project)"),
    "build.gradle.kts": ("gradle", "./gradlew --refresh-dependencies build (or sync the Gradle project)"),
    "gradle.lockfile": ("gradle", "./gradlew --refresh-dependencies build (or sync the Gradle project)"),
    "libs.versions.toml": ("gradle", "./gradlew --refresh-dependencies build (or sync the Gradle project)"),
    "requirements.txt": ("pip", "pip install -r requirements.txt"),
    "poetry.lock": ("poetry", "poetry install"),
    "uv.lock": ("uv", "uv sync"),
    "Pipfile.lock": ("pipenv", "pipenv sync"),
    "go.sum": ("go", "go mod download"),
    "Cargo.lock": ("cargo", "cargo build"),
    "Gemfile.lock": ("bundler", "bundle install"),
    "composer.lock": ("composer", "composer install"),
}


def dependency_changes(changed_files: Iterable[str]) -> List[Dict[str, str]]:
    """``[{folder, kind, command, file}]`` for each project folder whose
    dependencies the change alters, one entry per folder and kind."""
    seen = set()
    out: List[Dict[str, str]] = []
    for path in changed_files or ():
        path = str(path).replace("\\", "/")
        name = posixpath.basename(path)
        if name not in _REINSTALL:
            continue
        folder = posixpath.dirname(path)
        if name == "libs.versions.toml" and folder.endswith("gradle"):
            folder = posixpath.dirname(folder)
        kind, command = _REINSTALL[name]
        key = (folder, kind)
        if key in seen:
            continue
        seen.add(key)
        out.append({"folder": folder or ".", "kind": kind, "command": command, "file": path})
    return out


def reinstall_note(changes: List[Dict[str, str]]) -> str:
    """Markdown for a PR body, or "" when nothing needs reinstalling."""
    if not changes:
        return ""
    lines = ["### Dependencies changed",
             "Anyone running this project must reinstall after pulling, or it runs against the old packages:"]
    for c in changes:
        where = "the repository root" if c["folder"] == "." else f"`{c['folder']}/`"
        lines.append(f"- in {where}: `{c['command']}` ({c['file']} changed)")
    return "\n".join(lines)

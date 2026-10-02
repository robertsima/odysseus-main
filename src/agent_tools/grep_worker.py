"""Standalone walk-and-search worker for the grep tool's no-ripgrep fallback.

STDLIB ONLY, and it must stay that way. 2026-10-01 production diagnostics: the
fallback used ``multiprocessing.get_context("spawn")`` with a target in
``src.agent_tools.filesystem_tools``, so every grep re-imported ``__main__``
and most of the app in the child (~10-12 s on the NAS CPU) before searching a
single file; consecutive greps landed exactly 12 s apart. Run as a script
(``python -I grep_worker.py``) this file imports only ``os``/``re``/``sys``/
``json``, so a worker starts in tens of milliseconds.

It stays a separate process rather than a thread of the server because the
user's regex runs here: a catastrophic pattern such as ``(a+)+$`` holds the GIL
inside ``re`` and cannot be interrupted, so in-process it would freeze the
whole server past the deadline. A process can be terminated.

Protocol: one JSON payload on stdin; JSON-array records on stdout, one per
line: ``["match", path, line_number, text]``, then ``["done"]`` or
``["error", message]``.
"""
import json
import os
import re
import sys
from typing import Optional

_CODENAV_MAX_LINE = 400


def _glob_to_regex(pat: str) -> "re.Pattern":
    """Translate a forward-slash glob (**, *, ?) into a compiled regex.
    `**/` matches zero or more complete directories.
    `*` matches within a single path segment (does not cross /).
    """
    i, n, out = 0, len(pat), []
    while i < n:
        if pat[i : i + 3] == "**/":
            out.append("(?:[^/]+/)*")
            i += 3
        elif pat[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out))


def run_grep(payload: dict, emit) -> None:
    """Walk ``payload["targets"]`` and ``emit`` one record per match.

    ``emit`` takes a tuple; the caller decides the transport (a stdout pipe
    for the script, a multiprocessing queue for frozen builds).
    """
    try:
        flags = re.IGNORECASE if payload["ignore_case"] else 0
        try:
            regex = re.compile(payload["pattern"], flags)
            glob_regex = (
                _glob_to_regex(payload["glob"].replace("\\", "/"))
                if payload["glob"]
                else None
            )
        except re.error as exc:
            emit(("error", f"grep: bad pattern: {exc}"))
            return

        requested_root = payload["root"]
        skip_dirs = set(payload["skip_dirs"])
        sensitive = {name.casefold() for name in payload["sensitive_names"]}
        max_hits = payload["max_hits"]
        max_line = payload.get("max_line", _CODENAV_MAX_LINE)
        hits = 0

        def within(path: str, root: str) -> bool:
            try:
                return os.path.commonpath(
                    [os.path.normcase(path), os.path.normcase(root)]
                ) == os.path.normcase(root)
            except ValueError:
                return False

        def safe_file(path: str, target: str) -> Optional[str]:
            if os.path.islink(path):
                return None
            canonical = os.path.realpath(path)
            if not within(canonical, requested_root) or not within(canonical, target):
                return None
            parts = [part.casefold() for part in canonical.split(os.sep)]
            if any(part in sensitive for part in parts):
                return None
            try:
                if not os.path.isfile(canonical) or os.stat(canonical).st_nlink > 1:
                    return None
            except OSError:
                return None
            return canonical

        for target in payload["targets"]:
            if hits >= max_hits:
                break
            if os.path.isfile(target):
                file_iter = iter((target,))
            else:
                def walk_files():
                    for directory, dirnames, filenames in os.walk(
                        target, followlinks=False
                    ):
                        dirnames[:] = [
                            name
                            for name in dirnames
                            if name not in skip_dirs
                            and name.casefold() not in sensitive
                            and not os.path.islink(os.path.join(directory, name))
                        ]
                        for name in filenames:
                            yield os.path.join(directory, name)

                file_iter = walk_files()

            for candidate in file_iter:
                path = safe_file(candidate, target)
                if path is None:
                    continue
                relative = os.path.relpath(path, requested_root).replace(os.sep, "/")
                if glob_regex and not (
                    glob_regex.fullmatch(relative)
                    or glob_regex.fullmatch(os.path.basename(path))
                ):
                    continue
                try:
                    with open(path, "r", encoding="utf-8", errors="strict") as handle:
                        for number, line in enumerate(handle, 1):
                            if regex.search(line):
                                emit((
                                    "match",
                                    path,
                                    number,
                                    line.rstrip()[:max_line],
                                ))
                                hits += 1
                                if hits >= max_hits:
                                    break
                except (UnicodeDecodeError, OSError):
                    continue
                if hits >= max_hits:
                    break
        emit(("done",))
    except BaseException as exc:
        try:
            emit(("error", f"grep: fallback worker failed: {exc}"))
        except BaseException:
            pass


def _main() -> None:
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    out = sys.stdout

    def emit(record: tuple) -> None:
        # ensure_ascii keeps odd filenames (lone surrogates) off the pipe's
        # text encoding, which differs per platform.
        out.write(json.dumps(record) + "\n")
        out.flush()

    run_grep(payload, emit)


if __name__ == "__main__":
    _main()

"""Import SKILL.md bundles from public GitHub (or skills.sh → GitHub) URLs."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

import httpx

from src.url_safety import check_outbound_url

logger = logging.getLogger(__name__)

MAX_FILES = 64
MAX_TOTAL_BYTES = 2_000_000
MAX_FILE_BYTES = 400_000
ALLOWED_SUFFIXES = (
    ".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sh", ".toml",
    ".js", ".ts", ".css", ".html", ".xml", ".csv",
)
TEXT_NAMES = {"skill.md", "license", "license.md", "readme.md"}
_GITHUB_HOSTS = frozenset({
    "github.com", "www.github.com", "api.github.com", "raw.githubusercontent.com",
})


def _github_host(url: str) -> str:
    return (urlparse(str(url)).hostname or "").lower()


def _assert_github_url(url: str, *, context: str = "URL") -> None:
    parsed = urlparse(str(url))
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in _GITHUB_HOSTS:
        raise SkillImportError(
            f"{context} must stay on HTTPS GitHub (got {host or 'unknown host'})"
        )


@dataclass
class ResolvedSource:
    owner: str
    repo: str
    ref: str
    path: str  # directory or file path inside repo (no leading slash)
    skill_selector: str = ""


class SkillImportError(ValueError):
    pass


def _safe_relpath(rel: str) -> str:
    raw = str(rel or "")
    if "\x00" in raw:
        raise SkillImportError("unsafe path contains NUL")
    rel = raw.replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise SkillImportError(f"unsafe path: {rel!r}")
    parts = rel.split("/")
    _win_reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    for part in parts:
        if not part or part != part.strip() or part in {".", ".."} or ":" in part or part.endswith((".", " ")):
            raise SkillImportError(f"unsafe path: {rel!r}")
        if part.split(".", 1)[0].casefold() in _win_reserved:
            raise SkillImportError(f"unsafe reserved path: {rel!r}")
    return "/".join(parts)


def _is_text_file(name: str) -> bool:
    low = name.lower()
    if low in TEXT_NAMES:
        return True
    return any(low.endswith(s) for s in ALLOWED_SUFFIXES)


# Max redirect hops to follow manually while re-validating each one.
_MAX_FETCH_REDIRECTS = 5


def _check_fetch_url(url: str) -> None:
    """SSRF guard for skill-import fetches (defense-in-depth).

    Skill bundles only ever come from public GitHub, never an internal
    address, so block private/loopback/link-local targets on every hop —
    matching the hardened web-fetch path in
    ``services/search/content.py:_get_public_url`` rather than the lenient
    default used for admin-configured model endpoints.
    """
    ok, reason = check_outbound_url(url, block_private=True)
    if not ok:
        raise SkillImportError(reason)


def _get_checked(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: float = 30.0,
) -> httpx.Response:
    """GET that follows redirects manually, re-running the SSRF guard per hop.

    ``httpx``'s ``follow_redirects=True`` validates only the initial URL, so a
    ``3xx`` to an internal address (``169.254.169.254``, ``127.0.0.1``, …) would
    still be connected to before any post-hoc host check. Following redirects by
    hand lets us re-validate every hop, closing that blind-SSRF gap.
    """
    current = url
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        for _ in range(_MAX_FETCH_REDIRECTS + 1):
            _check_fetch_url(current)
            _assert_github_url(current, context="fetch URL")
            r = client.get(current, headers=headers)
            if r.status_code in (301, 302, 303, 307, 308):
                location = r.headers.get("location")
                if not location:
                    return r
                current = urljoin(str(r.url), location)
                continue
            return r
    raise SkillImportError("too many redirects while fetching skill bundle")


def parse_skill_source(url: str) -> ResolvedSource:
    """Normalize skills.sh / GitHub web URLs into owner/repo/ref/path."""
    raw = (url or "").strip()
    if not raw:
        raise SkillImportError("URL is required")

    parsed = urlparse(raw)
    host = _github_host(raw)
    if host in {"skills.sh", "www.skills.sh"}:
        if parsed.scheme != "https":
            raise SkillImportError("skills.sh imports require HTTPS")
        bits = [p for p in parsed.path.split("/") if p]
        if len(bits) != 3:
            raise SkillImportError("skills.sh URL must be https://skills.sh/<owner>/<repo>/<skill>")
        owner, repo, selector = bits
        return ResolvedSource(owner=owner, repo=repo, ref="main", path="", skill_selector=selector)
    if host not in _GITHUB_HOSTS:
        raise SkillImportError(
            "Only GitHub URLs are supported (https://github.com/... or raw.githubusercontent.com/...)"
        )
    if parsed.scheme != "https":
        raise SkillImportError("GitHub imports require HTTPS")

    if host == "raw.githubusercontent.com":
        # /owner/repo/ref/path/to/file
        bits = [p for p in parsed.path.split("/") if p]
        if len(bits) < 4:
            raise SkillImportError("Invalid raw GitHub URL")
        owner, repo, ref = bits[0], bits[1], bits[2]
        path = "/".join(bits[3:])
        return ResolvedSource(owner=owner, repo=repo, ref=ref, path=path)

    bits = [p for p in parsed.path.split("/") if p]
    if len(bits) < 2:
        raise SkillImportError("Invalid GitHub URL")
    owner, repo = bits[0], bits[1]
    ref = "main"
    path = ""

    if len(bits) >= 4 and bits[2] in ("tree", "blob"):
        ref = bits[3]
        path = "/".join(bits[4:])
    elif len(bits) == 2:
        path = ""
    else:
        raise SkillImportError("GitHub URL must include /tree/<branch>/... or /blob/<branch>/...")

    return ResolvedSource(owner=owner, repo=repo, ref=ref, path=path)


def _raw_url(src: ResolvedSource, rel_path: str) -> str:
    rel = _safe_relpath(rel_path)
    return f"https://raw.githubusercontent.com/{src.owner}/{src.repo}/{quote(src.ref, safe='')}/{quote(rel, safe='/')}"


def _api_contents_url(src: ResolvedSource, rel_path: str = "") -> str:
    rel = _safe_relpath(rel_path) if rel_path else ""
    base = f"https://api.github.com/repos/{src.owner}/{src.repo}/contents"
    if rel:
        base += f"/{quote(rel, safe='/')}"
    return f"{base}?ref={quote(src.ref, safe='')}"


def _github_response_error(response: httpx.Response) -> SkillImportError:
    """Turn a failed GitHub HTTP response into a user-visible import error."""
    status = response.status_code
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = str(body.get("message") or "").strip()
    except Exception:
        detail = (response.text or "").strip()[:200]

    low = detail.lower()
    if status == 403 and "rate limit" in low:
        return SkillImportError(
            "GitHub API rate limit exceeded — try again in a bit"
            + (f" ({detail})" if detail else "")
        )
    if status == 404:
        return SkillImportError("path not found on GitHub")
    if detail:
        return SkillImportError(f"GitHub request failed ({status}): {detail}")
    return SkillImportError(f"GitHub request failed ({status})")


def _fetch_bytes(url: str) -> bytes:
    r = _get_checked(url, headers={"Accept": "application/vnd.github+json"}, timeout=30.0)
    if r.status_code >= 400:
        raise _github_response_error(r)
    _assert_github_url(str(r.url), context="redirect target")
    if len(r.content) > MAX_FILE_BYTES:
        raise SkillImportError(f"file too large: {url}")
    return r.content


def _fetch_text(url: str) -> str:
    data = _fetch_bytes(url)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SkillImportError(f"non-text file: {url}") from e


def _list_github_dir(src: ResolvedSource, rel_dir: str, out: Dict[str, str], *, depth: int = 0) -> None:
    if depth > 4:
        raise SkillImportError("skill bundle exceeds directory depth limit")
    if len(out) >= MAX_FILES:
        raise SkillImportError("skill bundle exceeds file count limit")
    url = _api_contents_url(src, rel_dir)
    r = _get_checked(url, headers={"Accept": "application/vnd.github+json"}, timeout=30.0)
    if r.status_code >= 400:
        raise _github_response_error(r)
    _assert_github_url(str(r.url), context="redirect target")
    entries = r.json()
    if not isinstance(entries, list):
        raise SkillImportError("expected a directory on GitHub")
    total = sum(len(v.encode("utf-8")) for v in out.values())
    for ent in entries:
        if len(out) >= MAX_FILES:
            raise SkillImportError("skill bundle exceeds file count limit")
        if total >= MAX_TOTAL_BYTES:
            raise SkillImportError("skill bundle exceeds size limit")
        if not isinstance(ent, dict):
            continue
        name = ent.get("name") or ""
        ent_type = ent.get("type")
        rel = _safe_relpath(f"{rel_dir}/{name}" if rel_dir else name)
        if ent_type == "dir":
            _list_github_dir(src, rel, out, depth=depth + 1)
            total = sum(len(v.encode("utf-8")) for v in out.values())
            continue
        if ent_type != "file":
            raise SkillImportError(f"unsupported resource type {ent_type!r}: {rel}")
        if not _is_text_file(name):
            raise SkillImportError(f"unsupported non-text resource in skill bundle: {rel}")
        dl = ent.get("download_url")
        if not dl:
            raise SkillImportError(f"skill resource has no download URL: {rel}")
        _assert_github_url(dl, context="download URL")
        text = _fetch_text(dl)
        total += len(text.encode("utf-8"))
        if total > MAX_TOTAL_BYTES:
            raise SkillImportError("skill bundle exceeds size limit")
        out[rel] = text


def _skill_candidates(files: Dict[str, str]) -> List[str]:
    return sorted(rel for rel in files if rel.rsplit("/", 1)[-1].casefold() == "skill.md")


def _select_skill(files: Dict[str, str], selector: str = "") -> str:
    candidates = _skill_candidates(files)
    if not candidates:
        raise SkillImportError("No SKILL.md found — link to a skill folder or SKILL.md on GitHub")
    if selector:
        wanted = _safe_relpath(selector).casefold().rstrip("/")
        exact = [p for p in candidates if p.casefold() == wanted or p.casefold() == f"{wanted}/skill.md"]
        if not exact:
            exact = [p for p in candidates if p.rsplit("/", 1)[0].split("/")[-1].casefold() == wanted]
        if len(exact) != 1:
            raise SkillImportError(
                f"skill selector {selector!r} did not identify exactly one skill; candidates: "
                + ", ".join(candidates)
            )
        return exact[0]
    if len(candidates) != 1:
        raise SkillImportError("multiple skills found; choose one explicitly: " + ", ".join(candidates))
    return candidates[0]


def _rebase_bundle(files: Dict[str, str], selected: str) -> Dict[str, str]:
    parent = selected.rsplit("/", 1)[0] if "/" in selected else ""
    prefix = f"{parent}/" if parent else ""
    rebased = {}
    for rel, content in files.items():
        if parent and not rel.startswith(prefix):
            continue
        local = rel[len(prefix):] if prefix else rel
        rebased[_safe_relpath(local)] = content
    return rebased


def _fetch_selected_location(src: ResolvedSource, selector: str) -> Tuple[Dict[str, str], str]:
    """Resolve a named skill without recursively downloading the repository."""
    wanted = _safe_relpath(selector).rstrip("/")
    locations = []
    if wanted.casefold().endswith("skill.md"):
        locations.append(wanted.rsplit("/", 1)[0] if "/" in wanted else "")
    elif "/" in wanted:
        locations.append(wanted)
    else:
        locations.extend([
            f".agents/skills/{wanted}",
            f".promptscript/skills/{wanted}",
            f"skills/{wanted}",
            wanted,
        ])
    seen = set()
    not_found = []
    for directory in locations:
        if directory in seen:
            continue
        seen.add(directory)
        skill_path = f"{directory}/SKILL.md" if directory else "SKILL.md"
        try:
            text = _fetch_text(_raw_url(src, skill_path))
        except SkillImportError as exc:
            if "path not found" in str(exc):
                not_found.append(skill_path)
                continue
            raise
        files: Dict[str, str] = {skill_path: text}
        if directory:
            _list_github_dir(src, directory, files)
        return files, skill_path
    raise SkillImportError(
        f"skill selector {selector!r} was not found; checked: " + ", ".join(not_found)
    )


def fetch_skill_bundle(url: str, skill: Optional[str] = None) -> Tuple[Dict[str, str], ResolvedSource]:
    """Download SKILL.md and sibling text assets. Returns relative_path → content."""
    src = parse_skill_source(url)
    files: Dict[str, str] = {}

    path = _safe_relpath(src.path) if src.path else ""
    selector = skill or src.skill_selector
    if selector and not path:
        files, selected = _fetch_selected_location(src, selector)
        return _rebase_bundle(files, selected), src
    if path.lower().endswith("skill.md"):
        parent = "/".join(path.split("/")[:-1])
        _list_github_dir(src, parent, files)
        selected = _select_skill(files, path)
        return _rebase_bundle(files, selected), src

    if path:
        _list_github_dir(src, path, files)
    else:
        _list_github_dir(src, "", files)
    selected = _select_skill(files, skill or src.skill_selector)
    return _rebase_bundle(files, selected), src


def pick_skill_md(files: Dict[str, str]) -> Tuple[str, str]:
    selected = _select_skill(files)
    return selected, files[selected]


def default_category_from_source(src: ResolvedSource) -> str:
    return "imported"

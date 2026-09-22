"""Claude Code cloud runner: delegate to Claude Code in GitHub Actions.

The local runner (``claude_code_tools``) starts the ``claude`` binary inside
this container, which then has to be signed in inside the container, and any
credential placed in the container environment is within reach of the agent's
own shell whenever the shell sandbox is unavailable. This runner avoids both.
Odysseus dispatches ``integrations/claude/github/odysseus-claude.yml`` in an
allowlisted repository. Anthropic's official Claude Code GitHub Action runs
there with the operator's own credential from that repository's secrets (set
up with Anthropic's own ``claude setup-token`` or ``/install-github-app``).
Odysseus never sees, stores or forwards it; see
skills/dev/claude-code-delegation/references/terms-and-boundaries.md.

The workflow, not Claude, does the git plumbing: it runs Claude on a prepared
``claude/odysseus-<id>`` branch with file tools and read-only git, then commits,
pushes and opens a draft pull request, and uploads Claude's closing summary as
the ``odysseus-result`` artifact. Odysseus finds the run by its ``run-name``,
follows it, and reports the result, branch and pull request back to the chat,
the Agents panel and the Workbench.

GitHub access reuses the agent worktree's credential (GitHub App installation
token, else ``ODYSSEUS_AGENT_GITHUB_TOKEN``). It needs Actions: write,
Contents: read and Pull requests: read on each allowlisted repository.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
import uuid
import zipfile
from typing import Any, Dict, List, Optional

from src import agent_activity as activity

logger = logging.getLogger(__name__)

WORKFLOW_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "integrations", "claude", "github", "odysseus-claude.yml",
)
DEFAULT_WORKFLOW = "odysseus-claude.yml"
RESULT_ARTIFACT = "odysseus-result"
RESULT_FILE = "claude-result.md"
BRANCH_PREFIX = "claude/odysseus-"
TASK_PREFIX = "cloud-"
MAX_PROMPT_CHARS = 20000
POLL_INTERVAL_S = 20.0
WATCH_LIMIT_S = 2 * 3600
API_TIMEOUT_S = 30.0
_MAX_TASKS = 200

_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_WORKFLOW_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}\.ya?ml$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_TERMINAL = {"completed", "failed", "cancelled"}

_tasks: Dict[str, Dict[str, Any]] = {}
_watchers: Dict[str, asyncio.Task] = {}
_loaded = False


class CloudError(RuntimeError):
    """A cloud-runner request that cannot go ahead, with the reason."""


# ── configuration ─────────────────────────────────────────────────────────
def _setting(key: str, default=None):
    try:
        from src.settings import get_setting
        value = get_setting(key, default)
        return default if value is None else value
    except Exception:
        return default


def repositories() -> List[str]:
    """Allowlisted ``owner/repo`` slugs Claude may be dispatched to."""
    raw = _setting("claude_cloud_repositories", []) or []
    if isinstance(raw, str):
        raw = re.split(r"[\s,]+", raw)
    out: List[str] = []
    for item in raw if isinstance(raw, list) else []:
        slug = str(item or "").strip().strip("/")
        if slug.lower().startswith("https://github.com/"):
            slug = slug[len("https://github.com/"):].removesuffix(".git").strip("/")
        if _SLUG_RE.fullmatch(slug) and slug.lower() not in {s.lower() for s in out}:
            out.append(slug)
    return out


def workflow_file() -> str:
    name = str(_setting("claude_cloud_workflow", DEFAULT_WORKFLOW) or DEFAULT_WORKFLOW).strip()
    return name if _WORKFLOW_RE.fullmatch(name) else DEFAULT_WORKFLOW


def is_cloud_repository(value: str) -> bool:
    slug = str(value or "").strip()
    return any(slug.lower() == repo.lower() for repo in repositories())


def workflow_template() -> str:
    with open(WORKFLOW_TEMPLATE, encoding="utf-8") as fh:
        return fh.read()


# ── GitHub ────────────────────────────────────────────────────────────────
def _github_config():
    from src.agent_worktree.config import load_config
    return load_config()


def _credential_blockers(cfg) -> List[str]:
    if not cfg.has_any_credential:
        return ["No GitHub credential: configure the GitHub App (ODYSSEUS_GITHUB_APP_ID, "
                "ODYSSEUS_GITHUB_APP_INSTALLATION_ID, ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH) "
                f"or {cfg.fallback_token_env}."]
    if cfg.has_github_app and not os.path.isfile(cfg.private_key_path):
        return ["ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH does not point at a file."]
    return []


async def _gh(method: str, path: str, *, params: Optional[Dict] = None,
              json_body: Optional[Dict] = None, raw: bool = False) -> Any:
    """One GitHub REST call with the agent worktree's credential."""
    import httpx

    from src.agent_worktree.github import resolve_token, scrub

    cfg = _github_config()
    blockers = _credential_blockers(cfg)
    if blockers:
        raise CloudError(blockers[0])
    token = await resolve_token(cfg)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=API_TIMEOUT_S, follow_redirects=raw) as client:
        resp = await client.request(method, f"{cfg.api_base}{path}", params=params,
                                    json=json_body, headers=headers)
    if resp.status_code >= 400:
        raise CloudError(f"GitHub {method} {path.split('?', 1)[0]} failed ({resp.status_code}): "
                         f"{scrub(resp.text, token)[:300]}")
    if raw:
        return resp.content
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        raise CloudError("GitHub returned a non-JSON response")


async def _download_artifact(url: str) -> bytes:
    """Artifact archives redirect to a signed storage URL that must NOT get
    the GitHub token, so the redirect is followed without it."""
    import httpx

    from src.agent_worktree.github import resolve_token

    token = await resolve_token(_github_config())
    async with httpx.AsyncClient(timeout=API_TIMEOUT_S, follow_redirects=False) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}",
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28"})
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
            resp = await client.get(resp.headers["location"])
    if resp.status_code >= 400:
        raise CloudError(f"artifact download failed ({resp.status_code})")
    return resp.content


# ── task records ──────────────────────────────────────────────────────────
def _store_path() -> str:
    from src import constants
    return os.path.join(constants.DATA_DIR, "claude_cloud", "tasks.json")


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(_store_path(), encoding="utf-8") as fh:
            rows = json.load(fh)
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and str(row.get("task_id", "")).startswith(TASK_PREFIX):
                _tasks[row["task_id"]] = row
    except (OSError, ValueError):
        pass


def _save() -> None:
    rows = sorted(_tasks.values(), key=lambda r: r.get("created_at") or 0)[-_MAX_TASKS:]
    path = _store_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("claude cloud: could not save task records: %s", exc)


def is_cloud_task(task_id: str) -> bool:
    return str(task_id or "").startswith(TASK_PREFIX)


def get(task_id: str, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    _load()
    record = _tasks.get(str(task_id or ""))
    if record is None or (owner and record.get("owner") and record.get("owner") != owner):
        return None
    return record


def summaries(owner: Optional[str] = None) -> List[Dict[str, Any]]:
    _load()
    rows = [r for r in _tasks.values() if not owner or not r.get("owner") or r.get("owner") == owner]
    rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    keys = ("task_id", "repository", "status", "created_at", "finished_at", "branch", "pr_url", "run_url", "label")
    return [{k: r.get(k) for k in keys} for r in rows[:50]]


# ── dispatch / follow ─────────────────────────────────────────────────────
async def dispatch(repository: str, prompt: str, *, base_branch: str = "", owner: Optional[str] = None,
                   session_id: Optional[str] = None, label: Optional[str] = None) -> Dict[str, Any]:
    """Start the workflow for one task; returns the task record at once."""
    _load()
    repo = next((r for r in repositories() if r.lower() == str(repository or "").strip().lower()), None)
    if repo is None:
        allowed = ", ".join(repositories()) or "none configured"
        raise CloudError(f"{repository!r} is not an allowlisted cloud repository ({allowed}). "
                         "Add it under Settings › Tools › Claude Code › Cloud runner.")
    prompt = str(prompt or "").strip()
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise CloudError(f"prompt is required and must be <= {MAX_PROMPT_CHARS} characters")
    base_branch = str(base_branch or "").strip()
    if base_branch and not _BRANCH_RE.fullmatch(base_branch):
        raise CloudError("base_branch is not a valid branch name")
    info = await _gh("GET", f"/repos/{repo}")
    ref = str((info or {}).get("default_branch") or "main")
    short = uuid.uuid4().hex[:12]
    await _gh("POST", f"/repos/{repo}/actions/workflows/{workflow_file()}/dispatches",
              json_body={"ref": ref, "inputs": {"task_id": short, "prompt": prompt, "base_branch": base_branch}})
    task_id = TASK_PREFIX + short
    title = f"Claude Code (cloud) · {repo}: {' '.join(prompt.split())[:80]}"
    run_id = activity.run_started(
        session_id, "claude_code", title, owner=owner,
        data={"backend": "cloud", "repository": repo, "cloud_task_id": task_id,
              "branch": BRANCH_PREFIX + short, "base_branch": base_branch or ref},
        detail=prompt[:1500],
    )
    record = {
        "task_id": task_id, "short_id": short, "repository": repo, "status": "queued",
        "created_at": time.time(), "finished_at": None, "owner": owner, "session_id": session_id,
        "label": label, "prompt": prompt[:2000], "base_branch": base_branch or ref,
        "branch": BRANCH_PREFIX + short, "workflow_run_id": None, "run_url": None,
        "branch_url": None, "pr_url": None, "pr_number": None, "result": None,
        "conclusion": None, "error": None, "activity_run_id": run_id,
    }
    _tasks[task_id] = record
    _save()
    _start_watcher(task_id)
    return record


async def _find_run(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = await _gh("GET", f"/repos/{record['repository']}/actions/workflows/{workflow_file()}/runs",
                     params={"event": "workflow_dispatch", "per_page": 30})
    wanted = f"Odysseus task {record['short_id']}"
    for run in (data or {}).get("workflow_runs") or []:
        if str(run.get("display_title") or run.get("name") or "").strip() == wanted:
            return run
    return None


async def _read_result(repo: str, run_id: int) -> Optional[str]:
    data = await _gh("GET", f"/repos/{repo}/actions/runs/{run_id}/artifacts")
    for art in (data or {}).get("artifacts") or []:
        if art.get("name") == RESULT_ARTIFACT and not art.get("expired"):
            blob = await _download_artifact(str(art.get("archive_download_url")))
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                names = [n for n in zf.namelist() if n.endswith(RESULT_FILE)]
                if names:
                    return zf.read(names[0]).decode("utf-8", "replace")[:20000]
    return None


async def _read_outputs(record: Dict[str, Any]) -> None:
    repo, branch = record["repository"], record["branch"]
    owner_login = repo.split("/", 1)[0]
    try:
        pulls = await _gh("GET", f"/repos/{repo}/pulls",
                          params={"head": f"{owner_login}:{branch}", "state": "all", "per_page": 5})
        if pulls:
            record["pr_url"] = pulls[0].get("html_url")
            record["pr_number"] = pulls[0].get("number")
    except CloudError as exc:
        logger.info("claude cloud: PR lookup failed for %s: %s", record["task_id"], exc)
    try:
        await _gh("GET", f"/repos/{repo}/branches/{branch}")
        record["branch_url"] = f"https://github.com/{repo}/tree/{branch}"
    except CloudError:
        record["branch_url"] = None
    if record.get("workflow_run_id"):
        try:
            record["result"] = await _read_result(repo, int(record["workflow_run_id"]))
        except (CloudError, zipfile.BadZipFile, OSError) as exc:
            logger.info("claude cloud: result artifact unavailable for %s: %s", record["task_id"], exc)


async def refresh(task_id: str) -> Optional[Dict[str, Any]]:
    """Bring one record up to date with GitHub. Terminal records are left alone."""
    record = get(task_id)
    if record is None or record.get("status") in _TERMINAL:
        return record
    run = await _find_run(record)
    if run is None:
        # The dispatch has not produced a run yet (GitHub creates it within
        # seconds); give up if it never appears.
        if time.time() - (record.get("created_at") or 0) > 600:
            _finish(record, "failed", error="GitHub never started the workflow run. Is "
                    f".github/workflows/{workflow_file()} on the default branch?")
        return record
    record["workflow_run_id"] = run.get("id")
    record["run_url"] = run.get("html_url")
    status = run.get("status")
    if status != "completed":
        if record.get("status") != "running" and status == "in_progress":
            record["status"] = "running"
            activity.publish(record.get("session_id"), "status", "Claude Code (cloud) is running",
                             source="claude_code", run_id=record.get("activity_run_id"),
                             owner=record.get("owner"), data={"status": "running", "run_url": record["run_url"]})
        _save()
        return record
    record["conclusion"] = run.get("conclusion")
    await _read_outputs(record)
    outcome = {"success": "completed", "cancelled": "cancelled"}.get(str(run.get("conclusion")), "failed")
    _finish(record, outcome, error=None if outcome == "completed" else f"workflow run {run.get('conclusion')}")
    return record


def _finish(record: Dict[str, Any], status: str, *, error: Optional[str] = None) -> None:
    record["status"] = status
    record["finished_at"] = time.time()
    if error:
        record["error"] = error
    _save()
    result = record.get("result") or ""
    where = record.get("pr_url") or record.get("branch_url") or record.get("run_url") or ""
    suffix = {"completed": "finished", "failed": "failed", "cancelled": "cancelled"}.get(status, status)
    activity.run_finished(
        record.get("session_id"), "claude_code", record.get("activity_run_id") or "",
        f"Claude Code (cloud) · {record['repository']} — {suffix}", status=status, owner=record.get("owner"),
        data={"backend": "cloud", "repository": record["repository"], "cloud_task_id": record["task_id"],
              "branch": record["branch"] if record.get("branch_url") else None,
              "pr_url": record.get("pr_url"), "run_url": record.get("run_url"),
              "result_excerpt": result[:400] or None, "error": (record.get("error") or "")[:400] or None},
        detail=(result or record.get("error") or where)[:2000] or None,
    )


def _start_watcher(task_id: str) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if task_id in _watchers and not _watchers[task_id].done():
        return
    _watchers[task_id] = loop.create_task(_watch(task_id))


async def _watch(task_id: str) -> None:
    deadline = time.monotonic() + WATCH_LIMIT_S
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                record = await refresh(task_id)
            except CloudError as exc:
                logger.info("claude cloud: refresh of %s failed: %s", task_id, exc)
                continue
            if record is None or record.get("status") in _TERMINAL:
                return
        record = get(task_id)
        if record is not None and record.get("status") not in _TERMINAL:
            _finish(record, "failed", error="Stopped following the run after two hours; check it on GitHub.")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("claude cloud: watcher for %s stopped", task_id, exc_info=True)
    finally:
        _watchers.pop(task_id, None)


async def wait(task_id: str, seconds: int) -> Optional[Dict[str, Any]]:
    deadline = time.monotonic() + max(0, seconds)
    record = await refresh(task_id)
    while record is not None and record.get("status") not in _TERMINAL and time.monotonic() < deadline:
        await asyncio.sleep(min(POLL_INTERVAL_S, max(1.0, deadline - time.monotonic())))
        record = await refresh(task_id)
    return record


async def cancel(task_id: str, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
    record = get(task_id, owner)
    if record is None or record.get("status") in _TERMINAL:
        return record
    run = await _find_run(record)
    if run is not None and run.get("status") != "completed":
        await _gh("POST", f"/repos/{record['repository']}/actions/runs/{run.get('id')}/cancel")
        record["workflow_run_id"] = run.get("id")
        record["run_url"] = run.get("html_url")
    _finish(record, "cancelled")
    return record


def report(record: Dict[str, Any]) -> Dict[str, Any]:
    """What the delegating agent gets back."""
    status = record.get("status")
    out = {k: record.get(k) for k in ("task_id", "repository", "status", "run_url", "pr_url",
                                      "base_branch", "result", "error", "conclusion")}
    out["backend"] = "cloud"
    out["branch"] = record.get("branch") if record.get("branch_url") else None
    if status in _TERMINAL:
        out["exit_code"] = 0 if status == "completed" else 1
        if status == "completed" and not record.get("branch_url"):
            out["note"] = "Claude made no file changes, so no branch or pull request was created."
        elif status == "completed" and not record.get("pr_url"):
            out["note"] = ("The branch was pushed but no pull request opened; enable 'Allow GitHub Actions "
                           "to create and approve pull requests' in the repository's Actions settings.")
    else:
        out["exit_code"] = 0
        out["note"] = ("Claude Code is running in GitHub Actions. Poll with action=poll and this task_id "
                       "(wait_seconds up to 300), or keep working; the result is reported here when it ends.")
    return out


# ── status ────────────────────────────────────────────────────────────────
async def status() -> Dict[str, Any]:
    cfg = _github_config()
    repos = repositories()
    hints = list(_credential_blockers(cfg))
    rows = []
    if not repos:
        hints.append("Add the repositories Claude may work on under Settings › Tools › Claude Code › Cloud runner.")
    for repo in repos:
        row: Dict[str, Any] = {"repository": repo}
        if not _credential_blockers(cfg):
            try:
                await _gh("GET", f"/repos/{repo}/actions/workflows/{workflow_file()}")
                row["workflow"] = True
            except CloudError as exc:
                row["workflow"] = False
                row["error"] = str(exc)
                if "(404)" in str(exc):
                    row["error"] = (f".github/workflows/{workflow_file()} is not on the default branch, or the "
                                    "GitHub credential cannot see this repository.")
        rows.append(row)
    ready = bool(repos) and not _credential_blockers(cfg) and all(r.get("workflow") for r in rows)
    return {
        "backend": "cloud",
        "ready": ready,
        "workflow_file": workflow_file(),
        "repositories": rows,
        "credential": "github_app" if cfg.has_github_app else ("token" if cfg.has_any_credential else None),
        "hints": hints,
        "setup": ("Copy the workflow (GET /api/claude-code/cloud/workflow.yml) to .github/workflows/"
                  f"{workflow_file()} and add a CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) or "
                  "ANTHROPIC_API_KEY repository secret. Claude's credential stays in GitHub."),
    }

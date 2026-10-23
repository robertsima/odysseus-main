"""The Library: one list of notes, documents and vault files, grouped by what
each file is to the person (master, export, signed copy), not where it lives.

Plain ``def`` routes: FastAPI runs them in its thread pool, so the vault walk
and the database queries never stall the event loop.
"""

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from core.middleware import INTERNAL_TOOL_USER
from src import library_index as index
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

KINDS = ("all", "note", "document", "file", "project")


def _human_owner(request: Request) -> str:
    owner = require_user(request)
    if owner == INTERNAL_TOOL_USER:
        raise HTTPException(403, "The Library is available only to human UI sessions")
    return owner


def _vault_scan():
    """The cached vault walk, or None when no vault is mounted."""
    from routes.personal_routes import _vault_file_kind
    from src.artifact_roles import SUPPORTING_EXTS
    from src import rag_sensitivity
    from src.settings import get_setting

    root = os.path.realpath(rag_sensitivity.vault_root())
    if not os.path.isdir(root):
        return None
    # Notes are listed from the notes store; keep their files out of the
    # file list so nothing shows twice.
    exclude = [str(get_setting("notes_directory", "Notes") or "Notes"),
               str(get_setting("notes_archive_directory", "Notes/Archive") or "Notes/Archive")]

    def file_kind(name: str) -> Optional[str]:
        # What the vault tree lists, plus the receipts and previews that sit
        # beside a document. Those attach to a group and are not served raw.
        kind = _vault_file_kind(name)
        if kind is None and not name.startswith(("~$", "."))                 and os.path.splitext(name)[1].lower() in SUPPORTING_EXTS:
            return "supporting"
        return kind

    return index.scan_vault(root, file_kind, exclude)


def _document_rows(owner: str):
    from sqlalchemy import or_
    from core.database import Document, SessionLocal
    from routes.document.document_helpers import _owner_session_filter

    is_pdf = or_(
        Document.current_content.like('%<!-- pdf_source upload_id="%'),
        Document.current_content.like('%<!-- pdf_form_source upload_id="%'),
    )
    db = SessionLocal()
    try:
        q = db.query(
            Document.id, Document.title, Document.language, Document.updated_at, is_pdf.label("is_pdf"),
        ).filter(Document.is_active == True).filter(  # noqa: E712
            or_(Document.archived == False, Document.archived.is_(None))  # noqa: E712
        )
        q = _owner_session_filter(q, owner)
        return q.all()
    finally:
        db.close()


def _notes(owner: str):
    from src.notes_store import STORE

    return STORE.list(owner or None, allow_private=True)


def _sort_key(item: Dict[str, Any]) -> str:
    return item.get("updated_at") or ""


def setup_library_routes() -> APIRouter:
    router = APIRouter(prefix="/api/library", tags=["library"])

    @router.get("/items")
    def library_items(
        request: Request,
        kind: str = Query("all"),
        project: Optional[str] = Query(None),
        q: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> Dict[str, Any]:
        owner = _human_owner(request)
        if kind not in KINDS:
            raise HTTPException(400, f"kind must be one of {', '.join(KINDS)}")

        scan = _vault_scan()
        names = [p["name"] for p in index.project_summaries(scan)] if scan else []
        items: List[Dict[str, Any]] = []

        if scan and kind in ("all", "file", "project"):
            items.extend(index.vault_group_items(scan))
        if kind in ("all", "document", "project"):
            for row in _document_rows(owner):
                items.append(index.document_item(row, index.project_for_title(row.title or "", names)))
        if kind in ("all", "note", "project"):
            for note in _notes(owner):
                tags = (note.extra_frontmatter or {}).get("tags")
                items.append(index.note_item(note, index.project_for_note(note.label, tags, note.content, names)))

        if kind == "project":
            items = [i for i in items if i["project"]]
        if project:
            wanted = project.strip().casefold()
            items = [i for i in items if (i["project"] or "").casefold() == wanted]
        if q:
            items = [i for i in items if index.matches_query(i, q)]

        items.sort(key=_sort_key, reverse=True)
        return {
            "items": items[offset:offset + limit],
            "total": len(items),
            "projects": sorted(names, key=str.casefold),
            "vault_shared": True,
        }

    @router.get("/projects")
    def library_projects(request: Request) -> Dict[str, Any]:
        owner = _human_owner(request)
        scan = _vault_scan()
        if not scan:
            return {"projects": []}
        names = [p["name"] for p in index.project_summaries(scan)]
        counts: Dict[str, Dict[str, int]] = {}

        def bump(name: Optional[str], key: str) -> None:
            if name:
                counts.setdefault(name.casefold(), {}).setdefault(key, 0)
                counts[name.casefold()][key] += 1

        for item in index.vault_group_items(scan):
            bump(item["project"], "files")
        for row in _document_rows(owner):
            bump(index.project_for_title(row.title or "", names), "documents")
        for note in _notes(owner):
            tags = (note.extra_frontmatter or {}).get("tags")
            bump(index.project_for_note(note.label, tags, note.content, names), "notes")
        return {"projects": index.project_summaries(scan, counts)}

    return router

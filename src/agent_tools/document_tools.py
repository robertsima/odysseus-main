from typing import Any, Dict, List, Optional
import logging
import re
from src.constants import MAX_READ_CHARS
from src.tool_approvals import document_content_digest
from src.tool_utils import _parse_tool_args, get_upload_handler
from src.upload_handler import reserve_upload_references

logger = logging.getLogger(__name__)


def _missing_document_upload(owner: Optional[str], content: Any) -> Optional[str]:
    """Reserve explicit upload URLs before an agent persists document text."""
    return reserve_upload_references(get_upload_handler(), owner, content)

# ---------------------------------------------------------------------------
# Active document state
# ---------------------------------------------------------------------------
#
# The active document is tracked PER CHAT SESSION. It used to be one
# process-wide id, so a document opened or edited in one chat became the
# target of an argument-less edit_document in any other chat, and after a
# restart (pointer empty) the tools silently fell back to "the most recently
# updated document" — on 2026-09-28 that sent three edits to a different
# document that shared the requested note's title. Callers without a session
# (legacy helpers, unit tests) share the "" key.

_active_documents: Dict[str, str] = {}
_ACTIVE_DOCUMENTS_MAX = 4096
_active_model: Optional[str] = None


def _active_key(session_id: Optional[str]) -> str:
    return str(session_id) if session_id else ""


def set_active_document(doc_id: Optional[str], session_id: Optional[str] = None):
    """Set (or with ``None`` clear) the active document for one chat session."""
    key = _active_key(session_id)
    if not doc_id:
        _active_documents.pop(key, None)
        return
    _active_documents.pop(key, None)  # re-insert so the newest is last
    _active_documents[key] = str(doc_id)
    while len(_active_documents) > _ACTIVE_DOCUMENTS_MAX:
        _active_documents.pop(next(iter(_active_documents)), None)


def set_active_model(model: Optional[str]):
    """Set the current model name for version summaries."""
    global _active_model
    _active_model = model


def get_active_document(session_id: Optional[str] = None) -> Optional[str]:
    """The active document id for this chat session, if any."""
    return _active_documents.get(_active_key(session_id))


def clear_active_document(doc_id: Optional[str] = None, session_id: Optional[str] = None) -> bool:
    """Clear in-memory active-document pointers. Returns True if one was cleared.

    - ``session_id`` given: clear that session's pointer (only if it matches
      ``doc_id`` when one is given).
    - only ``doc_id`` given: clear every session pointing at that document, so
      a different active document is left untouched.
    - neither: clear all pointers.

    Called when a document is detached from its session or deleted (its tab is
    closed): without this, the stale pointer makes the last-resort doc-injection
    path re-surface a closed document in a later chat (#1160).
    """
    if session_id is not None:
        key = _active_key(session_id)
        current = _active_documents.get(key)
        if current and (doc_id is None or current == doc_id):
            _active_documents.pop(key, None)
            return True
        return False
    if doc_id is None:
        _active_documents.clear()
        return True
    keys = [k for k, v in _active_documents.items() if v == doc_id]
    for k in keys:
        _active_documents.pop(k, None)
    return bool(keys)


# ---------------------------------------------------------------------------
# Explicit document targets
# ---------------------------------------------------------------------------
#
# Native calls carry `document_id` as a structured argument; the text tool
# pipeline turns it into a first-line header of the tool content so it
# survives the ToolBlock(tool_type, content) shape, approvals (the header is
# part of the sealed content) and the text-fence tool syntax alike.

_DOC_ID_HEADER_RE = re.compile(
    r"\A[ \t]*<<<DOCUMENT_ID:[ \t]*([^\n>]*?)[ \t]*>>>[ \t]*(?:\r?\n|\Z)"
)
_DOC_LINK_RE = re.compile(r"#document-([A-Za-z0-9][A-Za-z0-9_-]*)")


def normalize_document_ref(ref: Any) -> str:
    """Reduce a document reference to its id when it is a link.

    Accepts a bare id, ``document-<id>``, ``#document-<id>`` and a markdown
    link ``[Title](#document-<id>)``. Anything else (e.g. a title) is returned
    stripped, for the caller to resolve.
    """
    s = str(ref or "").strip().strip("`'\"").strip()
    m = _DOC_LINK_RE.search(s)
    if m:
        return m.group(1)
    if s.lower().startswith("document-") and re.fullmatch(r"document-[A-Za-z0-9_-]+", s):
        return s[len("document-"):]
    return s


def with_document_id_header(content: str, document_id: Any) -> str:
    """Prefix tool content with the explicit-target header (no-op without an id)."""
    ref = normalize_document_ref(document_id) if document_id is not None else ""
    ref = ref.replace("\n", " ").replace(">", "").strip()
    if not ref:
        return content or ""
    return f"<<<DOCUMENT_ID: {ref}>>>\n{content or ''}"


def split_document_id_header(content: Any) -> tuple:
    """Return ``(document_ref or None, content without the header)``."""
    text = content if isinstance(content, str) else ("" if content is None else str(content))
    m = _DOC_ID_HEADER_RE.match(text)
    if not m:
        return None, text
    ref = normalize_document_ref(m.group(1))
    return (ref or None), text[m.end():]


_CANDIDATE_LIMIT = 10


def _iso(ts: Any) -> str:
    try:
        return ts.isoformat(timespec="seconds") if ts else ""
    except Exception:
        return str(ts or "")


def _candidate_rows(docs) -> List[Dict[str, str]]:
    rows = []
    for d in docs or []:
        rows.append({
            "id": str(getattr(d, "id", "") or ""),
            "title": str(getattr(d, "title", "") or ""),
            "updated_at": _iso(getattr(d, "updated_at", None)),
        })
    return rows


def _document_candidates(db, Document, owner: Optional[str], limit: int = _CANDIDATE_LIMIT):
    try:
        q = db.query(Document).filter(Document.is_active == True)
        q = _owned_document_query(q, Document, owner)
        return _candidate_rows(q.order_by(Document.updated_at.desc()).limit(limit).all())
    except Exception:
        logger.debug("document candidate listing failed", exc_info=True)
        return []


def _target_error(message: str, candidates: List[Dict[str, str]], **extra) -> Dict:
    lines = [message]
    if candidates:
        lines.append("Candidate documents (most recently updated first):")
        for c in candidates:
            lines.append(
                f"- [{c['title'] or 'Untitled'}](#document-{c['id']}) "
                f"— document_id: {c['id']}, updated {c['updated_at'] or 'unknown'}"
            )
        lines.append("Call the tool again with document_id set to the intended document's id.")
    else:
        lines.append("You have no documents to edit — use create_document to make one.")
    out = {
        "error": "\n".join(lines),
        "exit_code": 1,
        "needs_document_id": True,
        "document_candidates": candidates,
    }
    out.update(extra)
    return out


def _lookup_document_ref(db, Document, ref: str, owner: Optional[str]):
    """Resolve an explicit reference to exactly one owned document.

    Returns ``(doc, error_dict)``. Order: exact id, unique id prefix (8+
    chars), unique exact title (case-insensitive). Several matches are
    refused, never guessed.
    """
    ref = normalize_document_ref(ref)
    if not ref:
        return None, None
    doc = _get_owned_document(db, Document, ref, owner, active_only=True)
    if doc:
        return doc, None
    try:
        if len(ref) >= 8 and re.fullmatch(r"[0-9A-Fa-f-]+", ref):
            q = db.query(Document).filter(Document.is_active == True, Document.id.like(f"{ref.lower()}%"))
            matches = _owned_document_query(q, Document, owner).order_by(Document.updated_at.desc()).limit(_CANDIDATE_LIMIT + 1).all()
            if len(matches) == 1:
                return matches[0], None
            if len(matches) > 1:
                return None, _target_error(
                    f"document_id '{ref}' is ambiguous: it matches {len(matches)} documents. Pass the full id.",
                    _candidate_rows(matches[:_CANDIDATE_LIMIT]),
                )
        from sqlalchemy import func
        q = db.query(Document).filter(Document.is_active == True, func.lower(Document.title) == ref.lower())
        matches = _owned_document_query(q, Document, owner).order_by(Document.updated_at.desc()).limit(_CANDIDATE_LIMIT + 1).all()
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, _target_error(
                f"'{ref}' is ambiguous: {len(matches)} documents share that title. "
                "Pass the id of the one you mean as document_id.",
                _candidate_rows(matches[:_CANDIDATE_LIMIT]),
                ambiguous=True,
            )
    except Exception:
        logger.debug("document reference lookup failed for %r", ref, exc_info=True)
    return None, _target_error(
        f"Document '{ref}' was not found among your documents.",
        _document_candidates(db, Document, owner),
    )


def _resolve_target_document(db, Document, ctx: dict, explicit_ref: Optional[str], tool_name: str):
    """Pick the one document an edit/update/suggest acts on.

    Returns ``(doc, error_dict)``. Precedence:
      1. the sealed approval target (``ctx["doc_id"]``) — an explicit ref in
         the content must name that same document;
      2. an explicit ``document_id`` from the call;
      3. this chat session's active document.
    There is no "most recent document" fallback: without a target the call
    fails with a list of candidates so the model can pass document_id.
    """
    owner = ctx.get("owner")
    session_id = ctx.get("session_id")
    sealed_id = ctx.get("doc_id") or None

    if sealed_id:
        doc = _get_owned_document(db, Document, sealed_id, owner)
        if not doc:
            if ctx.get("expected_document_version") is not None:
                return None, _approved_document_version_error(None, ctx)
            return None, _target_error(
                f"Document '{sealed_id}' was not found among your documents.",
                _document_candidates(db, Document, owner),
            )
        if explicit_ref:
            ref = normalize_document_ref(explicit_ref)
            if ref != doc.id and not (len(ref) >= 8 and doc.id.startswith(ref)) and ref.lower() != (doc.title or "").strip().lower():
                return None, {
                    "error": (
                        f"{tool_name}: the call names document '{ref}' but the approved "
                        f"target is '{doc.title}' ({doc.id}). Request the edit again."
                    ),
                    "exit_code": 1,
                }
        return doc, None

    if explicit_ref:
        return _lookup_document_ref(db, Document, explicit_ref, owner)

    active_id = get_active_document(session_id)
    if active_id:
        doc = _get_owned_document(db, Document, active_id, owner, active_only=True)
        if doc:
            return doc, None
        return None, _target_error(
            f"{tool_name}: the document last active in this chat ({active_id}) is no longer "
            "available. Pass document_id to choose the document to change.",
            _document_candidates(db, Document, owner),
        )

    return None, _target_error(
        f"{tool_name}: no document_id was given and no document is open in this chat, "
        "so the target is unknown. Nothing was changed.",
        _document_candidates(db, Document, owner),
    )


def _document_summary(verb: str, title: Any, doc_id: Any) -> str:
    title_s = str(title or "Untitled")
    return f'{verb} document "{title_s}" (#document-{doc_id}) — link it as [{title_s}](#document-{doc_id})'


def resolve_document_for_approval(content: Any, owner: Optional[str], session_id: Optional[str], active_document: Any):
    """The document an edit/update/suggest call would target, for sealing an approval.

    Mirrors ``_resolve_target_document`` without a sealed id: the explicit
    ``document_id`` header wins, else the session's active document (the one
    injected this turn, or the one a previous call in this chat targeted).
    Returns a detached Document or None when there is no single target.
    """
    ref, _ = split_document_id_header(content)
    pointer = get_active_document(session_id)
    if not ref and active_document is not None and pointer in (None, getattr(active_document, "id", None)):
        return active_document
    try:
        from src.database import SessionLocal, Document
    except Exception:
        return None if ref else active_document
    db = SessionLocal()
    try:
        doc, _err = _resolve_target_document(
            db, Document, {"owner": owner, "session_id": session_id}, ref, "approval",
        )
        if doc is not None:
            try:
                db.expunge(doc)
            except Exception:
                pass
        return doc
    except Exception:
        logger.debug("approval target resolution failed", exc_info=True)
        return None
    finally:
        db.close()


def _owned_document_query(query, Document, owner: Optional[str]):
    if owner is None:
        # A bare Python `False` is not a valid SQL expression — SQLAlchemy 1.4
        # deprecates it and 2.0 raises ArgumentError. Use the SQL `false()`
        # literal to return zero rows for an unscoped (owner-less) query.
        from sqlalchemy import false
        return query.filter(false())
    return query.filter(Document.owner == owner)


def _get_owned_document(db, Document, doc_id: str, owner: Optional[str], active_only: bool = False):
    q = db.query(Document).filter(Document.id == doc_id)
    if active_only:
        q = q.filter(Document.is_active == True)
    q = _owned_document_query(q, Document, owner)
    return q.first()


def _most_recent_owned_document(db, Document, owner: Optional[str], active_only: bool = False):
    q = db.query(Document)
    if active_only:
        q = q.filter(Document.is_active == True)
    q = _owned_document_query(q, Document, owner)
    return q.order_by(Document.updated_at.desc()).first()


def _approved_document_version_error(doc: Any, ctx: dict) -> Optional[Dict]:
    """Reject a sealed document action when its target changed meanwhile."""
    expected_version = ctx.get("expected_document_version")
    expected_digest = (
        str(ctx.get("expected_document_digest") or "").strip().lower()
    )
    if expected_version is None and not expected_digest:
        return None
    try:
        version_unchanged = (
            expected_version is None
            or int(getattr(doc, "version_count", -1)) == int(expected_version)
        )
    except (TypeError, ValueError):
        version_unchanged = False
    content_unchanged = True
    if expected_digest:
        content_unchanged = (
            doc is not None
            and document_content_digest(getattr(doc, "current_content", ""))
            == expected_digest
        )
    if version_unchanged and content_unchanged:
        return None
    return {
        "error": (
            "The target document changed after this action was proposed. "
            "Review the latest version and request the edit again."
        ),
        "exit_code": 1,
        "document_changed": True,
    }


# ---------------------------------------------------------------------------
# Document tools — create/update/edit/suggest living documents
# ---------------------------------------------------------------------------

def _sniff_doc_language(text: str) -> str:
    """Best-effort detect a document's language from its content when the model
    didn't specify one. Defaults to 'markdown' (prose). Recognizes the common
    markup/code types the editor supports so e.g. an SVG isn't saved as markdown."""
    import json as _json, re as _re2
    s = (text or "").strip()
    if not s:
        return "markdown"
    head = s[:600]
    hl = head.lower()
    if _looks_like_email_document(s):
        return "email"
    # Markup (unambiguous)
    if "<svg" in hl:
        return "svg"
    if hl.startswith("<?xml"):
        return "xml"
    if (hl.startswith("<!doctype html") or hl.startswith("<html")
            or _re2.search(r"<(div|body|head|p|span|table|button|h[1-6]|ul|ol|li|img)\b", hl)):
        return "html"
    # JSON
    if s[0] in "{[":
        try:
            _json.loads(s)
            return "json"
        except Exception:
            pass
    # Shebang
    first = s.split("\n", 1)[0].strip().lower()
    if first.startswith("#!"):
        return "python" if "python" in first else "bash"
    # Code by strong leading signals (line-anchored so prose with stray words won't match)
    if _re2.search(r"(?m)^\s*(def \w|class \w|import \w|from \w[\w.]* import )", s):
        return "python"
    if _re2.search(r"(?m)^\s*(function \w|const \w|let \w|export |import .* from )", s):
        return "javascript"
    if _re2.search(r"(?mi)^\s*(select .* from |create table |insert into |update \w)", s):
        return "sql"
    if _re2.search(r"(?m)^[.#]?[\w-]+\s*\{[^{}]*:[^{}]*;", s):
        return "css"
    return "markdown"

def _looks_like_email_document(text: str = "", title: str = "") -> bool:
    import re as _re
    title_l = (title or "").strip().lower()
    if title_l in {"new email", "new mail", "new message"}:
        return True
    s = (text or "").lstrip()
    if "\n---\n" in s and _re.search(r"(?im)^To:\s*", s) and _re.search(r"(?im)^Subject:\s*", s):
        return True
    return bool(_re.search(r"(?im)^To:\s*", s) and _re.search(r"(?im)^Subject:\s*", s))

def _split_email_header_body(text: str) -> tuple[str, str]:
    if "\n---\n" in (text or ""):
        header, body = (text or "").split("\n---\n", 1)
        return header.rstrip(), body.strip()
    return (text or "").strip(), ""

def _split_email_reply_history(body: str) -> tuple[str, str]:
    """Split draft body from quoted/original email history.

    Email reply docs keep the original thread below the user's new reply. Models
    often rewrite only the fresh reply body; this helper keeps the historical
    block from being wiped when update_document/edit_document replaces content.
    """
    text = body or ""
    literal = "---------- Previous message ----------"
    literal_idx = text.find(literal)
    if literal_idx >= 0:
        return text[:literal_idx].strip(), text[literal_idx:].strip()
    patterns = [
        r"(?m)^On .+ wrote:\s*$",
        r"(?m)^> .+",
    ]
    starts = []
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            starts.append(m.start())
    if not starts:
        return text.strip(), ""
    idx = min(starts)
    return text[:idx].strip(), text[idx:].strip()

def _merge_email_headers(old_header: str, new_header: str) -> str:
    """Preserve routing/threading metadata if a model omits it."""
    protected = (
        "In-Reply-To", "References", "X-Source-UID", "X-Source-Folder",
        "X-Attachments", "X-Forward-Attachments",
    )
    lines = [l for l in (new_header or "").splitlines() if l.strip()]
    present = {l.split(":", 1)[0].strip().lower() for l in lines if ":" in l}
    for old_line in (old_header or "").splitlines():
        if ":" not in old_line:
            continue
        key = old_line.split(":", 1)[0].strip()
        if key in protected and key.lower() not in present:
            lines.append(old_line)
            present.add(key.lower())
    return "\n".join(lines).rstrip()

def _coerce_email_document_content(existing: str, incoming: str) -> str:
    """Keep email docs in the To/Subject/---/body shape even if a model writes
    only the body or dumps header labels without the separator."""
    import re as _re
    old = existing or ""
    new = (incoming or "").strip()
    old_header, old_body = _split_email_header_body(old)
    _, old_history = _split_email_reply_history(old_body)
    if "\n---\n" in new:
        new_header, new_body = _split_email_header_body(new)
        new_own, new_history = _split_email_reply_history(new_body)
        if old_history and not new_history:
            new_body = (new_own + "\n\n" + old_history).strip()
        return _merge_email_headers(old_header, new_header).rstrip() + "\n---\n" + new_body
    header = old_header if old_header else "To: \nSubject: "
    if _looks_like_email_document(new):
        lines = new.splitlines()
        last_header_idx = -1
        header_re = _re.compile(r"^(To|Cc|Bcc|Subject|In-Reply-To|References|X-Source-UID|X-Source-Folder|X-Attachments):", _re.I)
        for i, line in enumerate(lines):
            if header_re.match(line.strip()):
                last_header_idx = i
        body_lines = lines[last_header_idx + 1:] if last_header_idx >= 0 else lines
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        body = "\n".join(body_lines).strip()
    else:
        body = new
    _, incoming_history = _split_email_reply_history(body)
    if old_history and not incoming_history:
        body = (body.strip() + "\n\n" + old_history).strip()
    return header.rstrip() + "\n---\n" + body

def parse_edit_blocks(content: str) -> list:
    """Parse <<<FIND>>>...<<<REPLACE>>>...<<<END>>> blocks."""
    edits = []
    pattern = r'<<<FIND>>>\n(.*?)\n<<<REPLACE>>>\n(.*?)\n<<<END>>>'
    for m in re.finditer(pattern, content, re.DOTALL):
        edits.append({"find": m.group(1), "replace": m.group(2)})
    return edits

def parse_suggest_blocks(content: str) -> list:
    """Parse <<<FIND>>>...<<<SUGGEST>>>...<<<REASON>>>...<<<END>>> blocks."""
    suggestions = []
    _skip_phrases = ["no change", "clear", "fine as", "looks good", "no improvement", "keep as"]
    pattern = r'<<<FIND>>>\n(.*?)\n<<<SUGGEST>>>\n(.*?)\n<<<REASON>>>\n(.*?)\n<<<END>>>'
    for m in re.finditer(pattern, content, re.DOTALL):
        find_text = m.group(1)
        replace_text = m.group(2)
        reason = m.group(3).strip()
        # Skip no-op suggestions where find == replace or reason says no change
        if find_text.strip() == replace_text.strip():
            continue
        if any(phrase in reason.lower() for phrase in _skip_phrases):
            continue
        suggestions.append({
            "id": f"sugg-{len(suggestions)+1}",
            "find": find_text,
            "replace": replace_text,
            "reason": reason,
        })
    return suggestions


def _pdf_source_upload_id(content: str) -> Optional[str]:
    try:
        from src.pdf_form_doc import find_source_upload_id
        return find_source_upload_id(content or "")
    except Exception:
        return None


def _strip_pdf_editor_markers(content: str) -> str:
    """Turn a PDF-wrapper markdown doc into ordinary editable markdown.

    PDF docs use hidden HTML comments for source-upload links, form fields, and
    page annotations. Those comments are necessary for rendering/exporting the
    original PDF, but they make a derived AI text edit keep showing the original
    PDF preview. Remove only the editor plumbing and keep the readable text.
    """
    text = content or ""
    text = re.sub(r'(?im)^\s*<!--\s*pdf(?:_form)?_source\s+[^>]*-->\s*\n*', '', text)
    text = re.sub(r'\s*<!--\s*field=[^>]*-->', '', text)
    text = re.sub(r'\s*<!--\s*annotation\s+[^>]*-->', '', text)
    return text.strip()


def _create_pdf_text_derivative(db, *, source_doc, content: str, owner: Optional[str], summary: str,
                                session_id: Optional[str] = None) -> dict:
    import uuid
    from src.database import Document, DocumentVersion

    clean = _strip_pdf_editor_markers(content)
    title_base = (getattr(source_doc, "title", None) or "PDF").strip()
    title = title_base if title_base.lower().endswith("edited") else f"{title_base} edited"
    doc_id = str(uuid.uuid4())
    ver_id = str(uuid.uuid4())
    new_doc = Document(
        id=doc_id,
        session_id=getattr(source_doc, "session_id", None),
        title=title,
        language="markdown",
        current_content=clean,
        version_count=1,
        is_active=True,
        owner=owner if owner is not None else getattr(source_doc, "owner", None),
    )
    ver = DocumentVersion(
        id=ver_id,
        document_id=doc_id,
        version_number=1,
        content=clean,
        summary=summary,
        source="ai",
    )
    db.add(new_doc)
    db.add(ver)
    db.commit()
    set_active_document(doc_id, session_id)
    return {
        "action": "create",
        "doc_id": doc_id,
        "title": title,
        "language": "markdown",
        "content": clean,
        "version": 1,
        "source_doc_id": getattr(source_doc, "id", None),
        "document_summary": (
            _document_summary("Created", title, doc_id)
            + f" (a text copy of PDF document #document-{getattr(source_doc, 'id', '')}, which is unchanged)"
        ),
    }


class CreateDocumentTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        """Create a new document. Supports two formats:
        1) Line-based: line 1 = title, line 2 (optional) = language, rest = content
        2) XML-like tags: <title>...</title><language>...</language><content>...</content>
        Some models mix them — strip any XML-style tags and fall back to line parsing."""
        import uuid, re as _re
        from src.database import SessionLocal, Document, DocumentVersion, Session as DbSession

        raw = content or ""
        session_id = ctx.get("session_id")
        owner = ctx.get("owner")

        # Known languages the editor understands (match the <select> in HTML)
        _KNOWN_LANGS = {
            "python", "javascript", "typescript", "html", "css", "markdown", "json",
            "yaml", "bash", "sql", "rust", "go", "java", "c", "cpp", "xml", "toml",
            "ini", "ruby", "php", "csv", "email", "text", "plain", "svg",
        }

        # Try XML tag extraction first
        title = None
        language = None
        content = None
        mt = _re.search(r"<title>\s*(.*?)\s*</title>", raw, _re.DOTALL | _re.IGNORECASE)
        ml = _re.search(r"<language>\s*(.*?)\s*</language>", raw, _re.DOTALL | _re.IGNORECASE)
        mc = _re.search(r"<content>\s*(.*?)\s*</content>", raw, _re.DOTALL | _re.IGNORECASE)
        if mt or mc:
            title = mt.group(1).strip() if mt else None
            language = ml.group(1).strip().lower() if ml else None
            content = mc.group(1) if mc else None

        # Fall back to line-based parsing. First strip any stray XML-ish tags.
        if title is None or content is None:
            cleaned = _re.sub(r"</?(?:title|language|content)>", "", raw)
            lines = cleaned.strip().split("\n")
            if title is None:
                title = lines[0].strip() if lines else "Untitled"
                lines = lines[1:]
            # Only consume second line as language if it looks like a valid short lang token
            if language is None and lines:
                candidate = lines[0].strip().lower()
                if candidate and len(candidate) < 20 and " " not in candidate and candidate in _KNOWN_LANGS:
                    language = candidate
                    lines = lines[1:]
            if content is None:
                content = "\n".join(lines)

        # Validate language: must be in known set, else default based on content
        if language and language not in _KNOWN_LANGS:
            language = None
        if not language:
            # No explicit language — sniff it from the content so an SVG / HTML / JSON
            # / code document isn't silently saved as markdown. Prose → markdown.
            language = _sniff_doc_language(content)
        if _looks_like_email_document(content, title):
            language = "email"

        if not title:
            title = "Untitled"

        if not session_id:
            return {"error": "No session context for document creation", "exit_code": 1}

        db = SessionLocal()
        try:
            doc_id = str(uuid.uuid4())
            ver_id = str(uuid.uuid4())

            # Inherit ownership from the chat session so the doc survives that
            # session later being deleted (session_id → NULL).
            _sess = db.query(DbSession).filter(DbSession.id == session_id).first()
            if owner is not None and (not _sess or _sess.owner != owner):
                return {"error": "Cannot create document in another user's session", "exit_code": 1}
            _owner = _sess.owner if _sess else None

            missing_id = _missing_document_upload(_owner, content)
            if missing_id:
                return {
                    "error": f"Referenced upload is no longer available: {missing_id}",
                    "exit_code": 1,
                }

            doc = Document(
                id=doc_id,
                session_id=session_id,
                title=title,
                language=language,
                current_content=content,
                version_count=1,
                is_active=True,
                owner=_owner,
            )
            ver = DocumentVersion(
                id=ver_id,
                document_id=doc_id,
                version_number=1,
                content=content,
                summary=f"Created by {_active_model or 'AI'}",
                source="ai",
            )
            db.add(doc)
            db.add(ver)
            db.commit()

            set_active_document(doc_id, session_id)
            try:
                from src.event_bus import fire_event
                fire_event("document_created", _owner)
            except Exception:
                logger.debug("document_created event dispatch failed", exc_info=True)

            return {
                "action": "create",
                "doc_id": doc_id,
                "title": title,
                "language": language,
                "content": content,
                "version": 1,
                "document_summary": _document_summary("Created", title, doc_id),
            }
        except Exception as e:
            db.rollback()
            return {"error": f"Failed to create document: {e}", "exit_code": 1}
        finally:
            db.close()

class UpdateDocumentTool:    
    async def execute(self, content: str, ctx: dict) -> Dict:
        """Update an existing document. Content = full new document text."""
        import uuid
        from src.database import SessionLocal, Document, DocumentVersion

        explicit_ref, content = split_document_id_header(content)
        owner = ctx.get("owner")
        session_id = ctx.get("session_id")

        db = SessionLocal()
        try:
            doc, target_error = _resolve_target_document(
                db, Document, ctx, explicit_ref, "update_document",
            )
            if target_error:
                return target_error
            target_id = doc.id

            version_error = _approved_document_version_error(doc, ctx)
            if version_error:
                return version_error

            is_email_doc = doc.language == "email" or _looks_like_email_document(doc.current_content or "", doc.title or "")
            new_content = _coerce_email_document_content(doc.current_content or "", content) if is_email_doc else content.strip()
            if is_email_doc:
                doc.language = "email"

            missing_id = _missing_document_upload(owner, new_content)
            if missing_id:
                return {
                    "error": f"Referenced upload is no longer available: {missing_id}",
                    "exit_code": 1,
                }

            if not is_email_doc and _pdf_source_upload_id(doc.current_content or ""):
                return _create_pdf_text_derivative(
                    db,
                    source_doc=doc,
                    content=new_content,
                    owner=owner,
                    summary=f"Created from PDF edit by {_active_model or 'AI'}",
                    session_id=session_id,
                )

            new_ver = doc.version_count + 1
            ver = DocumentVersion(
                id=str(uuid.uuid4()),
                document_id=target_id,
                version_number=new_ver,
                content=new_content,
                summary=f"Updated by {_active_model or 'AI'}",
                source="ai",
            )
            doc.current_content = new_content
            doc.version_count = new_ver
            db.add(ver)
            db.commit()
            set_active_document(target_id, session_id)
            logger.info("update_document: updated doc id=%s title=%r (session=%s)", target_id, doc.title, session_id)

            return {
                "action": "update",
                "doc_id": target_id,
                "title": doc.title,
                "language": doc.language,
                "content": new_content,
                "version": new_ver,
                "document_summary": _document_summary("Updated", doc.title, target_id),
            }
        except Exception as e:
            db.rollback()
            return {"error": f"Failed to update document: {e}", "exit_code": 1}
        finally:
            db.close()

class EditDocumentTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        """Apply targeted FIND/REPLACE edits to an existing document."""
        import uuid
        from src.database import SessionLocal, Document, DocumentVersion

        explicit_ref, content = split_document_id_header(content)
        owner = ctx.get("owner")
        session_id = ctx.get("session_id")

        edits = parse_edit_blocks(content)
        if not edits:
            return {"error": "No valid <<<FIND>>>...<<<REPLACE>>>...<<<END>>> blocks found", "exit_code": 1}

        db = SessionLocal()
        try:
            # No "most recent document" fallback: guessing the target once sent
            # edits to a different document that shared the requested title.
            doc, target_error = _resolve_target_document(
                db, Document, ctx, explicit_ref, "edit_document",
            )
            if target_error:
                return target_error
            target_id = doc.id

            version_error = _approved_document_version_error(doc, ctx)
            if version_error:
                return version_error

            is_email_doc = doc.language == "email" or _looks_like_email_document(doc.current_content or "", doc.title or "")
            blank_find_edits = [e for e in edits if not (e.get("find") or "").strip()]
            if blank_find_edits:
                if is_email_doc:
                    replacement_body = (blank_find_edits[0].get("replace") or "").strip()
                    if not replacement_body:
                        return {"error": "No edits applied — blank FIND block had no replacement text", "exit_code": 1}
                    updated_content = _coerce_email_document_content(doc.current_content or "", replacement_body)
                    applied = 1
                    skipped = max(0, len(edits) - 1)
                    doc.language = "email"
                    missing_id = _missing_document_upload(owner, updated_content)
                    if missing_id:
                        return {
                            "error": f"Referenced upload is no longer available: {missing_id}",
                            "exit_code": 1,
                        }
                    new_ver = doc.version_count + 1
                    ver = DocumentVersion(
                        id=str(uuid.uuid4()),
                        document_id=target_id,
                        version_number=new_ver,
                        content=updated_content,
                        summary=f"Edited email body by {_active_model or 'AI'}",
                        source="ai",
                    )
                    doc.current_content = updated_content
                    doc.version_count = new_ver
                    db.add(ver)
                    db.commit()
                    set_active_document(target_id, session_id)
                    return {
                        "action": "edit",
                        "doc_id": target_id,
                        "title": doc.title,
                        "language": doc.language,
                        "content": updated_content,
                        "version": new_ver,
                        "applied": applied,
                        "skipped": skipped,
                        "document_summary": _document_summary("Edited", doc.title, target_id),
                    }
                return {"error": "No edits applied — FIND text cannot be blank", "exit_code": 1}

            updated_content = doc.current_content
            applied = 0
            skipped = 0
            for edit in edits:
                _find = edit["find"]
                if _find in updated_content:
                    updated_content = updated_content.replace(_find, edit["replace"], 1)
                    applied += 1
                else:
                    # Defensive: the active-doc context shows a "N\t" line-number
                    # gutter for reference. Weaker models sometimes copy that prefix
                    # into FIND. If the exact match failed, retry with a leading
                    # "<digits><tab>" stripped from each FIND line — but only use it
                    # when that stripped form actually matches, so we never corrupt a
                    # legitimately tab-prefixed document.
                    _stripped = "\n".join(re.sub(r"^\d+\t", "", _l) for _l in _find.split("\n"))
                    if _stripped != _find and _stripped in updated_content:
                        updated_content = updated_content.replace(_stripped, edit["replace"], 1)
                        applied += 1
                        logger.info("edit_document: matched after stripping line-number gutter from FIND")
                    else:
                        logger.warning(f"edit_document: FIND text not found, skipping: {_find[:80]!r}")
                        skipped += 1

            if applied == 0:
                return {"error": f"No edits applied — none of the FIND blocks matched the document content (skipped {skipped})", "exit_code": 1}

            missing_id = _missing_document_upload(owner, updated_content)
            if missing_id:
                return {
                    "error": f"Referenced upload is no longer available: {missing_id}",
                    "exit_code": 1,
                }

            if _pdf_source_upload_id(doc.current_content or ""):
                return _create_pdf_text_derivative(
                    db,
                    source_doc=doc,
                    content=updated_content,
                    owner=owner,
                    summary=f"Created from PDF edit by {_active_model or 'AI'} ({applied} edit(s))",
                    session_id=session_id,
                )

            new_ver = doc.version_count + 1
            ver = DocumentVersion(
                id=str(uuid.uuid4()),
                document_id=target_id,
                version_number=new_ver,
                content=updated_content,
                summary=f"Edited by {_active_model or 'AI'} ({applied} edit(s))",
                source="ai",
            )
            doc.current_content = updated_content
            doc.version_count = new_ver
            db.add(ver)
            db.commit()
            set_active_document(target_id, session_id)
            logger.info(
                "edit_document: edited doc id=%s title=%r (%d applied, session=%s)",
                target_id, doc.title, applied, session_id,
            )

            return {
                "action": "edit",
                "doc_id": target_id,
                "title": doc.title,
                "language": doc.language,
                "content": updated_content,
                "version": new_ver,
                "applied": applied,
                "skipped": skipped,
                "document_summary": _document_summary("Edited", doc.title, target_id),
            }
        except Exception as e:
            db.rollback()
            return {"error": f"Failed to edit document: {e}", "exit_code": 1}
        finally:
            db.close()

class SuggestDocumentTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        """Create inline suggestions for the active document WITHOUT modifying it."""
        from src.database import SessionLocal, Document

        explicit_ref, content = split_document_id_header(content)

        suggestions = parse_suggest_blocks(content)
        if not suggestions:
            return {"error": "No valid <<<FIND>>>...<<<SUGGEST>>>...<<<REASON>>>...<<<END>>> blocks found", "exit_code": 1}

        db = SessionLocal()
        try:
            doc, target_error = _resolve_target_document(
                db, Document, ctx, explicit_ref, "suggest_document",
            )
            if target_error:
                return target_error
            target_id = doc.id

            version_error = _approved_document_version_error(doc, ctx)
            if version_error:
                return version_error

            # Validate that FIND text exists in document
            valid = []
            for s in suggestions:
                if s["find"] in doc.current_content:
                    valid.append(s)
                else:
                    logger.warning(f"suggest_document: FIND text not found, skipping: {s['find'][:80]!r}")

            if not valid:
                return {"error": "No suggestions matched the document content", "exit_code": 1}

            set_active_document(target_id, ctx.get("session_id"))
            return {
                "action": "suggest",
                "doc_id": target_id,
                "title": doc.title,
                "suggestions": valid,
                "count": len(valid),
                "document_summary": _document_summary(
                    f"Added {len(valid)} suggestion(s) to", doc.title, target_id,
                ),
            }
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Document management tool (delete, list, organize)
# ---------------------------------------------------------------------------
class ManageDocumentTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        """Manage documents: list, read/view/open, delete, tidy.

        Output format mirrors `manage_session`: list rows include a
        clickable `[Title](#document-<id>)` anchor + relative timestamps
        so the user can click straight from chat to open the editor.
        """
        from core.database import SessionLocal, Document
        from datetime import datetime, timezone

        owner = ctx.get("owner")

        try:
            args = _parse_tool_args(content)
        except ValueError:
            return {"error": "Invalid JSON arguments", "exit_code": 1}

        action = args.get("action", "list")
        db = SessionLocal()

        def _rel(ts):
            if not ts:
                return 'never'
            try:
                now = datetime.now(timezone.utc) if ts.tzinfo is not None else datetime.utcnow()
                diff = (now - ts).total_seconds()
            except Exception:
                return 'unknown'
            if diff < 60: return 'just now'
            if diff < 3600: return f'{int(diff / 60)}m ago'
            if diff < 86400: return f'{int(diff / 3600)}h ago'
            if diff < 86400 * 7: return f'{int(diff / 86400)}d ago'
            return ts.strftime('%Y-%m-%d')

        try:
            if action == "list":
                q = db.query(Document).filter(Document.is_active == True)
                q = _owned_document_query(q, Document, owner)
                if args.get("search"):
                    q = q.filter(Document.title.ilike(f"%{args['search']}%"))
                if args.get("language"):
                    q = q.filter(Document.language == args["language"])
                docs = q.order_by(Document.updated_at.desc()).limit(args.get("limit", 50)).all()
                if not docs:
                    msg = "No documents found" + (f" matching '{args['search']}'" if args.get("search") else "") + "."
                    return {"response": msg, "documents": [], "exit_code": 0}
                lines = []
                items = []
                for i, d in enumerate(docs):
                    size = len(d.current_content or "")
                    lang = d.language or "text"
                    ts = getattr(d, 'updated_at', None) or getattr(d, 'created_at', None)
                    marker = " ← most recent" if i == 0 else ""
                    lines.append(
                        f"- [{d.title}](#document-{d.id}) — {lang}, {size} chars, updated {_rel(ts)}{marker}"
                    )
                    items.append({"id": d.id, "title": d.title, "language": lang, "size": size})
                header = f"Found {len(docs)} document(s), sorted most-recent first. Click a title to open:"
                return {
                    "response": header + "\n" + "\n".join(lines),
                    "documents": items,
                    "exit_code": 0,
                }

            elif action in ("read", "view", "open", "get"):
                doc_id = normalize_document_ref(args.get("document_id") or args.get("id") or args.get("uid"))
                if not doc_id:
                    return {"error": "Need document_id (use action=list to find one)", "exit_code": 1}
                doc = _get_owned_document(db, Document, doc_id, owner, active_only=True)
                if not doc:
                    return {"error": f"Document '{doc_id}' not found", "exit_code": 1}
                body = doc.current_content or ""
                try:
                    preview_limit = max(1, min(int(args.get("limit", MAX_READ_CHARS)), MAX_READ_CHARS))
                except (TypeError, ValueError):
                    preview_limit = MAX_READ_CHARS
                try:
                    offset = max(0, int(args.get("offset", 0) or 0))
                except (TypeError, ValueError):
                    offset = 0
                offset = min(offset, len(body))
                end = min(offset + preview_limit, len(body))
                truncated = end < len(body)
                preview = body[offset:end]
                if truncated:
                    preview += f"\n... (truncated, {len(body)} chars total; next_offset={end})"
                anchor = f"[{doc.title}](#document-{doc.id})"
                return {
                    "response": f"{anchor} — click to open in editor.\n\n```{doc.language or ''}\n{preview}\n```",
                    "document": {
                        "id": doc.id,
                        "title": doc.title,
                        "language": doc.language,
                        "size": len(body),
                        "content": preview,
                        "truncated": truncated,
                        "offset": offset,
                        "next_offset": end if truncated else None,
                    },
                    "exit_code": 0,
                }

            elif action == "delete":
                # Same rule as the edit tools: an explicit id, else this chat's
                # active document — never "whatever was updated last".
                explicit = args.get("document_id") or args.get("id") or args.get("uid")
                doc, target_error = _resolve_target_document(
                    db, Document,
                    {"owner": owner, "session_id": ctx.get("session_id")},
                    str(explicit) if explicit else None,
                    "manage_documents delete",
                )
                if target_error:
                    return target_error
                title = doc.title
                deleted_id = doc.id
                doc.is_active = False
                db.commit()
                clear_active_document(deleted_id)
                return {"response": f"Deleted document '{title}' (id {deleted_id})", "exit_code": 0}

            elif action == "tidy":
                from src.document_actions import run_document_tidy
                result = await run_document_tidy(owner or "")
                return {"response": result, "exit_code": 0}

            else:
                return {"error": f"Unknown action: {action}", "exit_code": 1}
        except Exception as e:
            logger.error(f"manage_documents error: {e}")
            return {"error": str(e), "exit_code": 1}
        finally:
            db.close()

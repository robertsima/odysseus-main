"""Notes-domain tool implementations.

Extracted from tool_implementations.py as part of slice 1 (#4082/#4071).
Holds the manage_notes tool (notes + checklists CRUD).
``src.tool_implementations`` re-exports these for backward compatibility.
"""
import json
import logging
import re
from typing import Dict, Optional

from src.tools._common import _parse_tool_args
from src.tool_utils import get_upload_handler
from src.upload_handler import reserve_upload_references

logger = logging.getLogger(__name__)


async def do_manage_notes(
    content: str,
    owner: Optional[str] = None,
    allow_private: bool = False,
) -> Dict:
    """Handle manage_notes tool calls: CRUD on notes and checklists."""
    import uuid as _uuid
    from src.notes_markdown import NoteItem, NoteRecord
    from src.notes_store import STORE

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}

    # Action aliases — match what models actually emit. `create` is the most
    # common alternative to `add`. Hyphenated forms also accepted.
    raw_action = (args.get("action") or "").replace("-", "_").strip().lower()
    action = raw_action
    _NOTE_ACTION_ALIASES = {
        "create": "add",
        "new": "add",
        "save": "add",
        "remind": "add",
        "remove": "delete",
        "remove_item": "toggle_item",
    }
    action = _NOTE_ACTION_ALIASES.get(action, action)

    def _store_list(*, archived=False, label=None):
        """Keep legacy/test store adapters working with private filtering."""
        try:
            return STORE.list(
                owner,
                archived=archived,
                label=label,
                allow_private=allow_private,
            )
        except TypeError:
            return STORE.list(owner, archived=archived, label=label)

    def _norm_note_title(value: str) -> str:
        text = (value or "").strip().lower()
        text = re.sub(r"^\s*reminder\s*:\s*", "", text)
        return re.sub(r"\s+", " ", text)

    def _note_by_prefix(note_id: str):
        try:
            return STORE.find(note_id, owner, allow_private=allow_private)
        except TypeError:
            return STORE.find(note_id, owner)

    def _format_note_list(notes) -> str:
        lines = []
        for n in notes:
            pin = " [PINNED]" if n.pinned else ""
            typ = " [checklist]" if n.note_type == "checklist" else ""
            lbl = f" #{n.label}" if n.label else ""
            title = n.title or "(untitled)"
            lines.append(f"- [{n.id[:8]}] **{title}**{pin}{typ}{lbl}")
            if n.note_type == "checklist" and n.items:
                for i, item in enumerate(n.items):
                    mark = "x" if item.done else " "
                    lines.append(f"  [{mark}] {i}: {item.text}")
            elif n.content:
                snippet = n.content[:80].replace("\n", " ")
                lines.append(f"  {snippet}")
        return "\n".join(lines)

    try:
        if action in ("list", "search", "find"):
            label_filter = str(args.get("label") or "").strip()
            if label_filter.lower() == "default":
                label_filter = ""
            show_archived = args.get("archived", False)
            notes = _store_list(archived=bool(show_archived), label=label_filter or None)
            if action in ("search", "find"):
                query = str(
                    args.get("query")
                    or args.get("text")
                    or args.get("title")
                    or args.get("content")
                    or ""
                ).strip().lower()
                if query:
                    filtered = []
                    for n in notes:
                        haystack = " ".join(
                            str(part or "")
                            for part in (n.title, n.content, n.label,
                                         " ".join(item.text for item in (n.items or [])))
                        ).lower()
                        if query in haystack:
                            filtered.append(n)
                    notes = filtered
            if not notes:
                return {"response": "No notes found.", "exit_code": 0}
            return {"results": _format_note_list(notes), "exit_code": 0}

        elif action == "view":
            note_id = args.get("id", "")
            note = _note_by_prefix(note_id)
            if not note:
                return {"error": "Note not found", "exit_code": 1}
            return {"results": _format_note_list([note]), "exit_code": 0}

        elif action == "add":
            # Accept the various field names models emit: `text` is the most
            # common stand-in for "title or body content" when the model
            # treats the note as a single string. If text was supplied and
            # neither title nor content, use it as the title.
            title = (args.get("title") or "").strip()
            content_raw = args.get("content")
            text_raw = args.get("text") or args.get("body")
            if not title and not content_raw and text_raw:
                title = text_raw.strip()
            elif not content_raw and text_raw:
                content_raw = text_raw
            # Accept both `items` (legacy/internal field) and `checklist_items`
            # (the schema-exposed name used by native function calls). Models
            # following the schema emit `checklist_items`; older code paths
            # and direct API callers still use `items`.
            items_raw = args.get("checklist_items")
            if items_raw is None:
                items_raw = args.get("items")
            items = None if items_raw is None else [
                NoteItem(
                    text=str(item.get("text") or "") if isinstance(item, dict) else str(item),
                    done=bool(item.get("done") or item.get("checked")) if isinstance(item, dict) else False,
                    extra={k: v for k, v in item.items() if k not in {"text", "done", "checked"}}
                    if isinstance(item, dict) else {},
                )
                for item in (items_raw if isinstance(items_raw, list) else [])
            ]
            items_json = json.dumps(items_raw) if items_raw is not None else None
            note_type = args.get("note_type", "checklist" if items_raw else "note")
            # Accept natural-language due_date ("tomorrow at 1pm") in
            # addition to ISO. Use the user-tz-aware parser so the LLM's
            # naive times ("today at 9pm") are anchored to the USER's clock,
            # not the server's. Returns ISO with explicit offset so frontend
            # `new Date()` resolves the right absolute moment regardless of
            # where the user is.
            due_raw = args.get("due_date")
            if not due_raw:
                combined_text = " ".join(
                    str(v or "")
                    for v in (title, content_raw, text_raw)
                ).strip()
                lower_combined = combined_text.lower()
                looks_like_reminder = (
                    raw_action in {"remind", "reminder"}
                    or re.search(r"\bremind(?:er)?\b", lower_combined)
                )
                if looks_like_reminder:
                    temporal = re.search(
                        r"\b(?:today|tonight|tomorrow|tmrw|yesterday)\b(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?"
                        r"|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s+(?:today|tonight|tomorrow|tmrw|yesterday)\b"
                        r"|\bin\s+\d+\s*(?:hour|hr|minute|min|day)s?\b",
                        lower_combined,
                    )
                    if temporal:
                        due_raw = temporal.group(0)
            due_iso = None
            if due_raw:
                try:
                    from routes.calendar_routes import parse_due_for_user as _pdt_user
                    due_iso = _pdt_user(due_raw)
                except Exception:
                    due_iso = due_raw  # fall through; trust the model
            if due_iso and title:
                # Calendar event reminders are represented as Notes. If the
                # model creates a calendar event with reminder_minutes and then
                # also creates a separate note reminder for the same title/time,
                # keep the existing note so the user gets only one dispatch.
                target_title = _norm_note_title(title)
                for existing in _store_list(archived=False)[:25]:
                    if existing.due_date != due_iso:
                        continue
                    if _norm_note_title(existing.title or "") == target_title:
                        return {
                            "response": f"Reminder already exists: \"{existing.title or title}\" (id: {existing.id[:8]})",
                            "note_id": existing.id,
                            "duplicate": True,
                            "exit_code": 0,
                        }
            missing_id = reserve_upload_references(
                get_upload_handler(),
                owner,
                content_raw,
                args.get("color"),
                items_json,
            )
            if missing_id:
                return {
                    "error": f"Referenced upload is no longer available: {missing_id}",
                    "exit_code": 1,
                }
            note = NoteRecord(
                id=str(_uuid.uuid4()),
                owner=owner,
                title=title,
                content=content_raw,
                items=items if note_type in {"checklist", "todo"} or items_raw is not None else None,
                color=args.get("color"),
                label=args.get("label"),
                pinned=args.get("pinned", False),
                due_date=due_iso,
                source="agent",
                session_id=args.get("session_id"),
            )
            STORE.save(note)
            # Return note_id so the chat-side renderer can build a real
            # "View note" button that opens the notes modal at this id.
            # Previously the create response only included a prose
            # confirmation; the model would type "View note" as a markdown
            # link with no target, leaving the user with a click that
            # did nothing and uncertainty about whether the note was made.
            return {
                "response": f"{'Reminder' if due_iso else 'Note'} created: \"{title or '(untitled)'}\" (id: {note.id[:8]})",
                "note_id": note.id,
                "note_title": title or "",
                "open_url": f"/#open=notes&note={note.id}",
                "exit_code": 0,
            }

        elif action == "update":
            note_id = args.get("id", "")
            note = _note_by_prefix(note_id)
            if not note:
                return {"error": "Note not found", "exit_code": 1}
            missing_id = reserve_upload_references(
                get_upload_handler(),
                owner,
                args.get("content"),
                args.get("color"),
                args.get("checklist_items"),
                args.get("items"),
            )
            if missing_id:
                return {
                    "error": f"Referenced upload is no longer available: {missing_id}",
                    "exit_code": 1,
                }
            for field in ("title", "content", "color", "label"):
                if field in args and args[field] is not None:
                    setattr(note, field, args[field])
            if "note_type" in args and args["note_type"] == "note":
                note.items = None
            elif "note_type" in args and args["note_type"] in {"checklist", "todo"} and note.items is None:
                note.items = []
            # Parse due_date the same way the `add` action does. The schema
            # advertises natural language ("tomorrow at 9am"), and naive ISO
            # strings need the user's tz offset attached so the frontend's
            # `new Date()` resolves the right absolute moment. Storing the raw
            # value here left updated reminders as unparseable literals that
            # never fired.
            if args.get("due_date") is not None:
                due_raw = args["due_date"]
                try:
                    from routes.calendar_routes import parse_due_for_user as _pdt_user
                    note.due_date = _pdt_user(due_raw)
                except Exception:
                    note.due_date = due_raw  # fall through; trust the model
            new_items = args.get("checklist_items")
            if new_items is None:
                new_items = args.get("items")
            if new_items is not None:
                note.items = [
                    NoteItem(text=str(item.get("text") or ""),
                             done=bool(item.get("done") or item.get("checked")),
                             extra={k: v for k, v in item.items() if k not in {"text", "done", "checked"}})
                    for item in new_items if isinstance(item, dict)
                ]
            if "pinned" in args:
                note.pinned = args["pinned"]
            if "archived" in args:
                note.archived = args["archived"]
            STORE.save(note)
            return {"response": f"Note updated: \"{note.title or '(untitled)'}\"", "exit_code": 0}

        elif action == "delete":
            note_id = args.get("id", "")
            note = _note_by_prefix(note_id)
            if not note:
                return {"error": "Note not found", "exit_code": 1}
            title = note.title
            STORE.delete(note.id, owner)
            return {"response": f"Deleted note: \"{title or '(untitled)'}\"", "exit_code": 0}

        elif action == "toggle_item":
            note_id = args.get("id", "")
            index = args.get("index", 0)
            note = _note_by_prefix(note_id)
            if not note:
                return {"error": "Note not found", "exit_code": 1}
            if not note.items:
                return {"error": "Note has no checklist items", "exit_code": 1}
            items = note.items
            if index < 0 or index >= len(items):
                return {"error": f"Item index {index} out of range (0-{len(items)-1})", "exit_code": 1}
            items[index].done = not items[index].done
            STORE.save(note)
            mark = "done" if items[index].done else "undone"
            return {"response": f"Item '{items[index].text}' marked {mark}", "exit_code": 0}

        else:
            return {"error": f"Unknown action: {action}. Use list/search/view/add/update/delete/toggle_item", "exit_code": 1}
    except Exception as e:
        logger.error(f"manage_notes error: {e}")
        return {"error": str(e), "exit_code": 1}

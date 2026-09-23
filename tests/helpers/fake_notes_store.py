from __future__ import annotations

from typing import Optional


class FakeNotesStore:
    """Small in-memory implementation of the Markdown notes-store contract."""

    def __init__(self, *notes):
        self.notes = {note.id: note for note in notes}
        self.saved = 0
        self.deleted = []

    @staticmethod
    def _belongs(note, owner: Optional[str]) -> bool:
        return owner is None or note.owner == owner

    def list(self, owner=None, *, archived=False, label=None, allow_private=True):
        # `allow_private` is honoured, not just accepted. The real store drops
        # private notes when it is False (src/notes_store.py), so a fake that
        # swallowed the argument would let a caller that asked to exclude
        # private notes still receive them — and the test would pass. A fake
        # that is wrong in the permissive direction about a privacy filter is
        # worse than one that does not take the argument at all.
        return [
            note for note in self.notes.values()
            if bool(note.archived) == bool(archived)
            and self._belongs(note, owner)
            and (allow_private or not self._is_private(note))
            and (not label or note.label == label)
        ]

    @staticmethod
    def _is_private(note):
        """Mirror the real store's private test without importing the resolver.

        The real one asks `resolve_sensitivity` about the note's path and
        frontmatter; a fake note carries neither reliably, so this reads the
        sensitivity the fixture set, defaulting to public.
        """
        extra = getattr(note, "extra_frontmatter", None) or {}
        return (getattr(note, "sensitivity", None)
                or extra.get("sensitivity")) == "private"

    def find(self, note_id, owner=None, *, allow_private=True):
        # Same contract as list(): the real find() takes allow_private and
        # withholds a private note when it is False. Not yet exercised by a
        # test, added with list()'s because the next caller to pass it would
        # otherwise get a TypeError or, worse, the note.
        matches = [
            note for key, note in self.notes.items()
            if key.startswith(str(note_id)) and self._belongs(note, owner)
            and (allow_private or not self._is_private(note))
        ]
        return matches[0] if len(matches) == 1 else None

    def save(self, note):
        self.notes[note.id] = note
        self.saved += 1
        return note

    def delete(self, note_id, owner=None):
        note = self.find(note_id, owner)
        if note is None:
            return False
        self.deleted.append(note.id)
        del self.notes[note.id]
        return True

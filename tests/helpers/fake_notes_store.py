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

    def list(self, owner=None, *, archived=False, label=None):
        return [
            note for note in self.notes.values()
            if bool(note.archived) == bool(archived)
            and self._belongs(note, owner)
            and (not label or note.label == label)
        ]

    def find(self, note_id, owner=None):
        matches = [
            note for key, note in self.notes.items()
            if key.startswith(str(note_id)) and self._belongs(note, owner)
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

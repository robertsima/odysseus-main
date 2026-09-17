"""Short-lived, bounded, owner-bound snapshots for reviewing untrusted skills.

Confirmation installs the exact files shown, never a changed remote revision.
Snapshots are intentionally process-local: after restart/expiry, preview again.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time

from .skill_importer import MAX_TOTAL_BYTES, pick_skill_md


class SkillImportReviews:
    def __init__(self, ttl=600, capacity=16, clock=time.monotonic):
        self.ttl, self.capacity, self.clock = ttl, capacity, clock
        self._pending = {}
        self._lock = threading.Lock()

    def inspect(self, files, *, owner, source_url):
        files = dict(files)
        size = sum(len(value.encode('utf-8')) for value in files.values())
        if size > MAX_TOTAL_BYTES:
            raise ValueError('Skill bundle exceeds size limit')
        skill_path, markdown = pick_skill_md(files)
        digest = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._pending = {key: value for key, value in self._pending.items() if value['expires'] > now}
            # Bound memory and prevent one operator from retaining unlimited imports.
            own = [key for key, value in self._pending.items() if value['owner'] == owner]
            for key in own[:-3]:
                self._pending.pop(key)
            while len(self._pending) >= self.capacity:
                self._pending.pop(next(iter(self._pending)))
            self._pending[token] = dict(files=files, owner=owner, source_url=source_url, expires=now + self.ttl)
        return {
            'review_token': token, 'sha256': digest, 'expires_in': self.ttl,
            'source_url': source_url, 'skill_path': skill_path,
            'files': [{'path': path, 'bytes': len(content.encode('utf-8')), 'content': content}
                      for path, content in sorted(files.items())],
            'markdown': markdown, 'total_bytes': size,
            'warnings': [
                'Third-party instructions are untrusted. Review all files before importing.',
                'Imported as a draft. No scripts, installers or model audit will run automatically.',
                'Publishing a skill does not grant tools, MCP connections or private-data access.',
            ],
        }

    def consume(self, token, *, owner):
        with self._lock:
            pending = self._pending.get(token)
            if not pending or pending['owner'] != owner:
                raise ValueError('Preview not found for this user. Preview the skill again.')
            if pending['expires'] <= self.clock():
                self._pending.pop(token)
                raise ValueError('Preview expired. Preview the skill again.')
            self._pending.pop(token)
            return pending

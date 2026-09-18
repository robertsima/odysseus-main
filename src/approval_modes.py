"""The fork's per-chat approval modes: vocabulary only, not yet enforced.

Chats, agent profiles and loadouts store an ``approval_mode``:

* ``auto``      -- nothing asks.
* ``ask_risky`` -- destructive or outward-facing actions ask first.
* ``ask_all``   -- every tool that can change something asks first.

The fork enforced these in its own agent loop and approval store. Both were
replaced by upstream's (2026-09-18), whose gate asks for an exact approval only
once untrusted content has influenced a run. Until the modes are re-ported
onto that gate, a stored mode is accepted and kept but changes nothing, so
`ENFORCED` is False and the settings API reports no selectable modes; the
frontend hides the controls on that signal rather than offering a switch that
does nothing.
"""

from __future__ import annotations

from typing import Optional

MODES = ("auto", "ask_risky", "ask_all")
DEFAULT_MODE = "auto"
ENFORCED = False


def normalize_mode(value: Optional[str]) -> str:
    value = str(value or "").strip().lower()
    return value if value in MODES else DEFAULT_MODE

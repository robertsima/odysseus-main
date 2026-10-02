"""What an agent turn has done so far, read off its own event stream.

A turn's tool calls and per-round text are saved when the turn ends. A turn
that is stopped part-way (the Stop button, a new message that replaces it, a
restart) used to keep only its visible text: on 2026-09-29 a 27-round run was
replaced by the user's "did you get stuck?", and the saved reply was two
sentences. The tool cards were gone after a reload, and the next turn could
not tell what it had been doing, nor that a bash command was still running.

The chat route feeds every event it forwards to ``TurnTrail`` and, when the
turn is stopped, saves ``stopped_record()`` with the partial reply.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

# Kept from a tool_output event, as the finished turn saves them.
_EVENT_KEYS = ("tool", "command", "output", "exit_code", "diff", "doc_id",
               "image_url", "image_prompt", "image_model", "image_size", "image_quality")
_COMMAND_IN_NOTE = 200


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min" if not secs or minutes >= 10 else f"{minutes} min {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def _one_line(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class TurnTrail:
    def __init__(self) -> None:
        self.round = 1
        self._texts: Dict[int, str] = {}
        self.tool_events: List[Dict] = []
        # Tools started and not answered yet, oldest first.
        self._running: List[Dict] = []

    def text(self, delta: str) -> None:
        if delta:
            self._texts[self.round] = self._texts.get(self.round, "") + delta

    def event(self, data: Dict, now: Optional[float] = None) -> None:
        kind = data.get("type")
        now = time.monotonic() if now is None else now
        if kind == "agent_step":
            try:
                self.round = max(self.round, int(data.get("round") or 1))
            except (TypeError, ValueError):
                pass
        elif kind == "tool_start":
            self._running.append({
                "round": self.round,
                "tool": str(data.get("tool") or ""),
                "command": str(data.get("command") or ""),
                "started": now,
                "tail": "",
            })
        elif kind == "tool_progress":
            if self._running and data.get("tail"):
                self._running[-1]["tail"] = str(data.get("tail"))
        elif kind == "tool_output":
            tool = str(data.get("tool") or "")
            started = next((r for r in self._running if r["tool"] == tool), None)
            if started is not None:
                self._running.remove(started)
            event = {"round": started["round"] if started else self.round}
            event.update({k: data[k] for k in _EVENT_KEYS if k in data})
            self.tool_events.append(event)

    def take_before(self, round_num: int) -> Tuple[List[str], List[Dict]]:
        """Remove and return what rounds before ``round_num`` produced.

        A steer splits the reply there (2026-10-02): the pieces so far are
        saved as their own assistant message, and what this trail reports from
        then on, including a stopped turn's record, is only what came after.
        Returns ``(round_texts, tool_events)``; the texts list has one entry
        per round, so an index is that round minus one, as in saved metadata.
        """
        texts = [self._texts.pop(r, "") for r in range(1, round_num)]
        taken = [e for e in self.tool_events if int(e.get("round") or 1) < round_num]
        self.tool_events = [e for e in self.tool_events if int(e.get("round") or 1) >= round_num]
        return texts, taken

    def has_work(self) -> bool:
        return bool(self.tool_events or self._running)

    def stopped_record(self, now: Optional[float] = None) -> Tuple[str, List[Dict], List[str]]:
        """``(note, tool_events, round_texts)`` for a turn stopped now.

        The note says what was cut off: it goes at the end of the saved
        reply, where the next turn reads it, and is the last of the round
        texts so a reload shows it below the tool cards. A tool still running
        is saved as a stopped tool card.
        """
        now = time.monotonic() if now is None else now
        events = list(self.tool_events)
        for run in self._running:
            events.append({
                "round": run["round"],
                "tool": run["tool"],
                "command": run["command"],
                "output": run["tail"],
                "exit_code": None,
                "stopped": True,
            })
        finished = len(self.tool_events)
        calls = f" {finished} tool call{'s' if finished != 1 else ''} had finished." if finished else ""
        if self._running:
            run = self._running[0]
            cmd = _one_line(run["command"], _COMMAND_IN_NOTE).replace("`", "'")
            what = f"`{run['tool']}` had been running for {_duration(now - run['started'])}"
            note = f"[Turn stopped while {what}" + (f": `{cmd}`" if cmd else "") + f".{calls}]"
        elif finished:
            note = f"[Turn stopped.{calls}]"
        else:
            note = ""
        last = max([self.round, *self._texts.keys()])
        texts = [self._texts.get(r, "") for r in range(1, last + 1)]
        if note:
            texts.append(note)
        return note, events, texts

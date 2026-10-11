"""Capture rules: read one line of text without a model.

Everything here is a pure function of the text and the person's local clock:

* `strip_when` / `resolve_phrase` find date and time phrases and resolve them.
* `classify` picks a route (calendar, task, waiting, outcome) or says the line
  is ambiguous or unreadable.
* `normalize_title` is the one key every duplicate check compares, applied to
  both the title being captured and the content already stored.

A route of `None` means the rules found nothing confident; the caller may ask
the utility model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

_I = re.IGNORECASE

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "tues": 1, "tue": 1, "wednesday": 2, "weds": 2, "wed": 2,
    "thursday": 3, "thurs": 3, "thur": 3, "thu": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sunday": 6,
}
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
           "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_WD = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_MON = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")

# Ordered: a phrase claimed by an earlier pattern is not read again by a later one.
_P_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?!\d)(?:[T ](\d{2}):(\d{2})(?::\d{2})?)?", _I)
_P_IN = re.compile(r"\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)\b", _I)
_P_NEXTWEEK = re.compile(r"\bnext\s+week\b", _I)
_P_MD = re.compile(rf"\b({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!:)", _I)
_P_DM = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MON})\b", _I)
_P_WD = re.compile(rf"\b(?:(next|this|coming)\s+)?({_WD})\b", _I)
_P_DAY = re.compile(r"\b(today|tonight|tomorrow|tmrw)\b", _I)
_P_T12 = re.compile(r"(?<![\w:])(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", _I)
_P_T24 = re.compile(r"(?<![\w:])([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*(?:am|pm))", _I)
_P_ATH = re.compile(r"\bat\s+(\d{1,2})\b(?!\s*(?::|\d|am\b|pm\b))", _I)
_P_NOON = re.compile(r"\b(noon|midnight)\b", _I)
_PREP_BEFORE = re.compile(r"\b(?:by|on|at|before|due|until|around)\s+$", _I)
_DANGLING = re.compile(r"\s+(?:for|by|on|at|before|until|due|from|around|to|and|then)$", _I)


@dataclass
class When:
    """A resolved date or date and time on the person's local wall clock (naive)."""
    start: datetime
    has_time: bool
    phrase: str = ""

    def iso(self) -> str:
        """Local ISO: a date for an all-day phrase, a date and time otherwise."""
        return self.start.strftime("%Y-%m-%dT%H:%M:%S") if self.has_time else self.start.strftime("%Y-%m-%d")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _tidy(text: str) -> str:
    text = _squash(text)
    text = re.sub(r"^[\s,;:\-–—]+|[\s,;:\-–—]+$", "", text)
    while True:
        trimmed = _DANGLING.sub("", text)
        if trimmed == text:
            break
        text = trimmed
    return (text[:1].upper() + text[1:]) if text else text


def _hour(h: int, ampm: Optional[str]) -> int:
    if ampm:
        h %= 12
        return h + 12 if ampm.lower() == "pm" else h
    return h


def strip_when(text: str, now: datetime) -> Tuple[str, Optional[When]]:
    """Return `text` without its date and time phrases, and the `When` they describe."""
    text = text or ""
    taken: List[Tuple[int, int]] = []
    day: Optional[datetime] = None
    clock: Optional[Tuple[int, int]] = None
    absolute: Optional[Tuple[datetime, bool]] = None
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def free(m: "re.Match") -> bool:
        return not any(m.start() < e and s < m.end() for s, e in taken)

    def claim(m: "re.Match") -> None:
        taken.append((m.start(), m.end()))

    for m in _P_ISO.finditer(text):
        if not free(m):
            continue
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        claim(m)
        if m.group(4) is not None:
            absolute = (d.replace(hour=int(m.group(4)), minute=int(m.group(5))), True)
        elif day is None and absolute is None:
            day = d
    for m in _P_IN.finditer(text):
        if not free(m) or absolute is not None:
            continue
        n, unit = int(m.group(1)), m.group(2).lower()
        claim(m)
        if unit.startswith(("min", "hour", "hr")):
            delta = timedelta(minutes=n) if unit.startswith("min") else timedelta(hours=n)
            absolute = (now.replace(second=0, microsecond=0) + delta, True)
        else:
            days = n * (7 if unit.startswith("week") else 1)
            day = today + timedelta(days=days)
    for m in _P_NEXTWEEK.finditer(text):
        if free(m) and day is None:
            claim(m)
            day = today + timedelta(days=7 - today.weekday())
    for m in _P_MD.finditer(text):
        if free(m) and day is None:
            d = _month_day(now, _MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
            if d:
                claim(m)
                day = d
    for m in _P_DM.finditer(text):
        if free(m) and day is None:
            d = _month_day(now, _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
            if d:
                claim(m)
                day = d
    for m in _P_WD.finditer(text):
        if free(m) and day is None:
            claim(m)
            ahead = (_WEEKDAYS[m.group(2).lower()] - today.weekday()) % 7 or 7
            day = today + timedelta(days=ahead)
    for m in _P_DAY.finditer(text):
        if free(m) and day is None:
            claim(m)
            word = m.group(1).lower()
            day = today + timedelta(days=1) if word in ("tomorrow", "tmrw") else today
    for m in _P_T12.finditer(text):
        if free(m) and clock is None:
            h, mi = int(m.group(1)), int(m.group(2) or 0)
            if 1 <= h <= 12 and mi < 60:
                claim(m)
                clock = (_hour(h, m.group(3)), mi)
    for m in _P_T24.finditer(text):
        if free(m) and clock is None:
            claim(m)
            clock = (int(m.group(1)), int(m.group(2)))
    for m in _P_NOON.finditer(text):
        if free(m) and clock is None:
            claim(m)
            clock = (12, 0) if m.group(1).lower() == "noon" else (0, 0)
    for m in _P_ATH.finditer(text):
        if free(m) and clock is None:
            h = int(m.group(1))
            if 1 <= h <= 12:
                claim(m)
                # A bare "at 3" means the afternoon; "at 8" and later means the morning.
                clock = (h + 12 if h <= 6 else (12 if h == 12 else h), 0)

    if not taken:
        return text, None

    when: When
    if absolute is not None:
        start, has_time = absolute
        if clock is not None and not has_time:
            start = start.replace(hour=clock[0], minute=clock[1])
            has_time = True
        when = When(start, has_time)
    elif day is not None and clock is not None:
        when = When(day.replace(hour=clock[0], minute=clock[1]), True)
    elif day is not None:
        when = When(day, False)
    else:
        start = today.replace(hour=clock[0], minute=clock[1])
        when = When(start if start > now else start + timedelta(days=1), True)

    pieces, cursor = [], 0
    for s, e in sorted(taken):
        head = text[cursor:s]
        prep = _PREP_BEFORE.search(head)  # a preposition right before a date goes with it
        pieces.append(head[:prep.start()] if prep else head)
        cursor = e
    pieces.append(text[cursor:])
    when.phrase = _squash(" ".join(text[s:e] for s, e in sorted(taken)))
    return _squash(" ".join(pieces)), when


def _month_day(now: datetime, month: int, day: int) -> Optional[datetime]:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for year in (now.year, now.year + 1):
        try:
            d = datetime(year, month, day)
        except ValueError:
            return None
        if d >= today:
            return d
    return None


def resolve_phrase(phrase: str, now: datetime) -> Optional[When]:
    """Resolve a phrase the client sent as `due` ("friday 3pm", "2026-10-16", ...)."""
    _, when = strip_when(_squash(phrase), now)
    return when


# ── routes ───────────────────────────────────────────────────────────────────

_LEAD = re.compile(
    r"^(?:please\s+)?(?:"
    r"remind me(?:\s+(?:to|about|that|of))?|reminder\s*[:\-]?|don'?t forget(?:\s+to)?|remember to|"
    r"add (?:a |an )?(?:new )?(?:task|todo|to-do|reminder)(?:\s+(?:to|for|about|called|named))?\s*[:\-]?|"
    r"create (?:a |an )?(?:new )?(?:task|todo|to-do|reminder)(?:\s+(?:to|for|about|called|named))?\s*[:\-]?|"
    r"to-?do\s*[:\-]|task\s*[:\-]|"
    r"i (?:need|have|got|must|should|ought) to|need to|have to|gotta"
    r")\s*", _I)

_IMPERATIVES = frozenset("""
call phone text email message ping buy get pick pay renew send book schedule fix write finish submit order
clean cancel check review file return pack prepare update upgrade install read draft reply respond sign print
mail ship charge wash water feed take bring drop grab find research fill apply register confirm sort tidy
change set arrange contact move refill restock replace repair deposit transfer vacuum mow ask tell remind
organize organise make do go visit see unsubscribe backup back finalize finalise sharpen defrost""".split())

_STRONG = re.compile(
    r"\b(?:meeting|lunch|dinner|breakfast|brunch|coffee with|interview|flight|appointment|reservation|"
    r"concert|party|wedding|stand-?up|webinar|conference|drinks|catch[- ]up|1:1|one-on-one|demo|"
    r"presentation|lesson|class|recital|call with|meet)\b", _I)
_WEAK = re.compile(
    r"\b(?:dentist|doctor|dr\.?|physio|therapist|haircut|vet|check-?up|mechanic|optician|massage|gym)\b", _I)
_WAIT = re.compile(r"\b(?:waiting\s+(?:on|for)|follow(?:ing)?[\s-]*up\s+with)\s+(?P<who>.+)", _I)
_WAIT_STOP = re.compile(r"\s+(?:about|regarding|re|for|to|on|until|by|that|because|so|and|from|with)\s+|[,;:]|\s[-–—]\s", _I)
_WAIT_TAIL = re.compile(r"[,;]?\s*(?:and\s+)?follow(?:ing)?[\s-]*up\s*$", _I)
_PROJECT = re.compile(r"\b(?:project|build|plan|launch|migrate|redesign|roll\s?out|set\s?up)\b", _I)
_MULTI = re.compile(
    r"\b(?:then|steps?|phases?|milestones?|stages?|roadmap|after that|first)\b|;|\band\b.*\band\b|,.*,", _I)


@dataclass
class Decision:
    """What the rules made of one line. `route` is calendar, task, waiting, outcome,
    clarify or None (the rules could not tell)."""
    route: Optional[str]
    title: str
    when: Optional[When] = None
    reminder: bool = False
    multi_step: bool = False
    waiting_on: Optional[str] = None
    question: Optional[str] = None
    alternatives: Tuple[str, ...] = field(default_factory=tuple)


def _first_word(text: str) -> str:
    m = re.match(r"[a-z']+", text.lower())
    return m.group(0) if m else ""


def classify(text: str, now: datetime) -> Decision:
    raw = _squash(text)
    lead = _LEAD.match(raw)
    body = raw[lead.end():] if lead else raw
    clean, when = strip_when(body, now)
    title = _tidy(clean)
    low = clean.lower()
    d = Decision(route=None, title=title, when=when, reminder=bool(lead and "remind" in lead.group(0).lower()))
    d.multi_step = bool(_PROJECT.search(low) and _MULTI.search(low))

    wait = _WAIT.search(clean)
    if wait:
        who = wait.group("who")
        stop = _WAIT_STOP.search(who)
        who = (who[:stop.start()] if stop else who).strip(" ,.;:")[:60]
        d.route, d.waiting_on = "waiting", who or None
        d.title = _tidy(_WAIT_TAIL.sub("", clean))
        return d
    if d.multi_step:
        d.route = "outcome"
        return d

    imperative = _first_word(low) in _IMPERATIVES and not re.match(r"call with\b", low)
    framed_as_task = bool(lead) or imperative
    cue_strong, cue_weak = bool(_STRONG.search(low)), bool(_WEAK.search(low))
    if (cue_strong or cue_weak) and not framed_as_task:
        if when and (when.has_time or cue_strong):
            d.route = "calendar"
        else:
            d.route = "clarify"
            d.alternatives = ("calendar", "task")
            d.question = "Is this an appointment to put on the calendar, or a task to do?"
        return d
    if framed_as_task:
        d.route = "task"
    return d


# ── duplicate key ────────────────────────────────────────────────────────────

_NORMALISE_NOW = datetime(2000, 1, 3)  # only which phrases are dates matters here, not which day it is


def normalize_title(text: str) -> str:
    """The key duplicate checks compare: lowercase words with the lead-in, labels, priorities and
    date phrases removed. Apply it to the captured title and to stored content alike."""
    s = _squash(text)
    s = re.sub(r"(?<!\S)[@#]\S+", " ", s)
    s = re.sub(r"(?<!\S)p[1-4](?!\S)", " ", s, flags=_I)
    lead = _LEAD.match(s)
    if lead:
        s = s[lead.end():]
    s, _ = strip_when(s, _NORMALISE_NOW)
    s = _squash(re.sub(r"[^\w\s]", " ", s.lower()))
    while True:
        trimmed = _DANGLING.sub("", s)
        if trimmed == s:
            return s
        s = trimmed

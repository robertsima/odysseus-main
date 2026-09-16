"""CalDAV → local SQLite sync.

The Settings UI lets users save CalDAV credentials, but the original
sync path was removed when calendar storage was migrated to SQLite.
This module re-wires that gap as a one-way pull (remote → local),
called on calendar open and from a periodic scheduler loop.

Design notes:
- We use the `caldav` lib so PROPFIND discovery + REPORT XML work
  across Radicale / Nextcloud / Apple / Fastmail without us
  reinventing the protocol. It's pure Python.
- The lib is synchronous; we run it in a threadpool via
  `asyncio.to_thread` so the FastAPI event loop stays free.
- Each remote calendar maps to one local `CalendarCal` row with
  `source="caldav"` and `id` = a stable hash of the remote URL so
  re-syncs idempotently target the same row.
- Events upsert by VEVENT UID (kept as the local `uid`). Local
  CalDAV-sourced events not seen in the latest pull are deleted so
  remote deletions propagate.
- Datetimes are converted to UTC and the row is flagged `is_utc=True`
  so the serializer adds the Z suffix and the frontend renders in the
  user's local TZ correctly.
"""

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import socket
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

from sqlalchemy.exc import IntegrityError

from core.log_safety import redact_url

logger = logging.getLogger(__name__)

# Pull window: 90 days back, 1 year forward. Keeps the REPORT cheap and
# matches what the calendar UI typically renders. Far-future recurring
# events still come through via RRULE expansion on the frontend.
_LOOKBACK_DAYS = 90
_LOOKAHEAD_DAYS = 365
_BLOCKED_HOSTS = {
    "localhost",
    "localhost.",
    "ip6-localhost",
    "metadata.google.internal",
}
GOOGLE_CALDAV_OAUTH_REQUIRED = (
    "Google Calendar CalDAV requires OAuth 2.0. The generic CalDAV "
    "username/password form only supports Basic Auth providers such as "
    "Radicale, Nextcloud, iCloud, and Fastmail."
)


def _private_caldav_allowed() -> bool:
    return os.environ.get("ODYSSEUS_ALLOW_PRIVATE_CALDAV", "0").lower() in {"1", "true", "yes"}


def _validate_caldav_address(addr: ipaddress._BaseAddress) -> None:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
    ):
        raise ValueError("CalDAV URL host is not allowed")
    if addr.is_private and not _private_caldav_allowed():
        raise ValueError("Private CalDAV IPs require ODYSSEUS_ALLOW_PRIVATE_CALDAV=1")


def _validate_caldav_ip(host: str) -> None:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return
    _validate_caldav_address(ip)


def _resolve_caldav_host_ips(host: str) -> list[ipaddress._BaseAddress]:
    addrs: list[ipaddress._BaseAddress] = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            addrs.append(ipaddress.ip_address(sockaddr[0].split("%", 1)[0]))
        except ValueError:
            continue
    return addrs


def _validate_caldav_hostname(host: str) -> None:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return
    except ValueError:
        pass
    try:
        addrs = _resolve_caldav_host_ips(host)
    except OSError:
        raise ValueError("CalDAV URL host does not resolve")
    if not addrs:
        raise ValueError("CalDAV URL host does not resolve")
    for addr in addrs:
        _validate_caldav_address(addr)


def validate_caldav_url(raw_url: str) -> str:
    """Validate and normalize a user-provided CalDAV URL before server-side use."""
    url = (raw_url if isinstance(raw_url, str) else "").strip()
    if not url:
        raise ValueError("CalDAV URL is required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("CalDAV URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("CalDAV URL must include a host")
    if parsed.username or parsed.password:
        raise ValueError("Put CalDAV credentials in the username/password fields, not the URL")
    if parsed.fragment:
        raise ValueError("CalDAV URL fragments are not allowed")
    try:
        parsed.port
    except ValueError:
        raise ValueError("CalDAV URL has an invalid port")
    host = (parsed.hostname or "").lower()
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise ValueError("CalDAV URL host is not allowed")
    _validate_caldav_ip(host)
    _validate_caldav_hostname(host)
    return urlunparse(parsed._replace(fragment=""))


def is_google_caldav_url(raw_url: str) -> bool:
    """Return True for Google's CalDAV endpoints.

    Google CalDAV no longer accepts Basic Auth, so these URLs need a dedicated
    OAuth provider instead of the generic username/password CalDAV path.
    """
    url = (raw_url if isinstance(raw_url, str) else "").strip()
    if not url:
        return False
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    if host.endswith("googleusercontent.com") and path.startswith("/caldav/v2/"):
        return True
    return host in {"www.google.com", "google.com"} and path.startswith("/calendar/dav/")


def _event_etag(obj) -> str:
    """Best-effort ETag extraction from python-caldav resources."""
    try:
        etag = getattr(obj, "etag", None)
        if callable(etag):
            etag = etag()
        return str(etag or "")
    except Exception:
        return ""


def _stable_cal_id(remote_url: str, owner: str = "", account_id: str = "") -> str:
    """Deterministic local id for a remote CalDAV calendar, scoped to owner
    and account so two users — or one user with two accounts — pointing at
    the same server URL get distinct local rows (avoids PK collision, #2765).
    The owner and account_id default to "" for the legacy/URL-only path so
    existing callers without those arguments keep working."""
    key = f"{owner}\n{account_id}\n{remote_url}"
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"caldav-{h}"


def _to_utc_naive(dt):
    """CalDAV datetimes can be tz-aware (with a TZID) or naive. The DB
    column is naive but we set is_utc=True so the serializer adds Z.
    All-day events stay as date and get widened to datetime here."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None), False
        return dt, False  # naive → treat as local
    # date-only (all-day)
    return datetime(dt.year, dt.month, dt.day), True


def _find_existing_event(db, pending, uid_val, calendar_id, owner=""):
    """Find the event to update for THIS calendar.

    CalendarEvent.uid is the global primary key, but CalDAV only guarantees a
    uid is unique *within a collection*. So the row holding a given uid may sit
    under a different calendar_id than the one being synced, and the insert we
    would otherwise emit is guaranteed to fail the PK.

    Three cases:
    * a row under THIS calendar → update it (the common path);
    * a row under another calendar OWNED BY THE SAME USER, or one whose calendar
      no longer exists → adopt it, letting the caller re-point calendar_id. This
      heals events stranded under a stale calendar id: _stable_cal_id hashes
      owner+account_id+url, so any change to that derivation orphans every
      existing row and leaves the sync retrying the same doomed inserts forever;
    * anything else — another owner's row, or an owner we cannot establish →
      None, exactly as before. Reassigning it would move (steal) that user's
      event into this calendar, the regression #2765 fixed. The caller then
      attempts an insert that the PK rejects, and the savepoint around it turns
      that into a skipped event rather than a failed batch.
    """
    from core.database import CalendarCal, CalendarEvent
    hit = pending.get(uid_val) or db.query(CalendarEvent).filter(
        CalendarEvent.uid == uid_val,
        CalendarEvent.calendar_id == calendar_id,
    ).first()
    if hit is not None:
        return hit

    other = db.query(CalendarEvent).filter(CalendarEvent.uid == uid_val).first()
    if other is None:
        return None
    other_cal = db.query(CalendarCal).filter(
        CalendarCal.id == other.calendar_id,
    ).first()
    if other_cal is None:
        return other          # orphaned row — its calendar is gone, safe to reclaim
    if owner and other_cal.owner == owner:
        return other          # same user, stale calendar id → adopt
    return None               # another owner (or owner unknown) → never adopt


def _google_caldav_events_url(url: str) -> str | None:
    """Map a Google CalDAV *principal* URL to its event-collection URL.

    Google serves the principal at ``…/user`` but events live under ``…/events``
    — the ``/user`` resource holds no VEVENTs. The `caldav` library's
    principal→home-set discovery does not reliably enumerate calendars from
    Google's ``/user`` endpoint, so the sync falls into the "treat the URL as a
    single calendar" fallback below. Pointed at ``/user`` that fallback issues
    every calendar-query REPORT against the principal, which returns a clean but
    empty 200 for all date ranges — the calendar shows no events even though
    auth succeeded (issue #2507).

    Both Google CalDAV endpoint forms are handled, since some accounts only
    authenticate against one of them:
      - newer:  ``https://apidata.googleusercontent.com/caldav/v2/<id>/user``
      - legacy: ``https://www.google.com/calendar/dav/<id>/user``

    Returns the events URL for a recognised Google principal URL, else None so
    the caller keeps the original URL unchanged.
    """
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    if not path.endswith("/user"):
        return None
    if not is_google_caldav_url(url):
        return None
    new_path = path[: -len("/user")] + "/events"
    return urlunparse(parts._replace(path=new_path))


def _google_calendar_collection_url(url: str) -> str | None:
    """Return Google's concrete event collection for principal or event URLs."""
    mapped = _google_caldav_events_url(url)
    if mapped:
        return mapped
    parts = urlparse(url)
    if is_google_caldav_url(url) and parts.path.rstrip("/").endswith("/events"):
        return url.rstrip("/")
    return None


def _open_url_as_calendar(client, url: str):
    """Open ``url`` as a single calendar collection.

    Used when principal discovery yields no calendars. Google's principal URL
    is not an event collection, so map it to the events URL first
    (see ``_google_caldav_events_url``); other servers' URLs are used as-is.
    """
    target = _google_calendar_collection_url(url) or url
    return client.calendar(url=target)


def _build_dav_client(url: str, username: str, password: str, token: str = ""):
    """Construct a CalDAV client with automatic redirects disabled.

    ``validate_caldav_url`` resolves and vets the *initial* host, but caldav's
    underlying HTTP session follows 3xx redirects by default. So a URL that
    passes validation can still be redirected — at request time — to
    loopback / link-local / private space, re-opening the SSRF the host check
    closes. Pin the session to zero redirects: any 3xx then raises instead of
    silently following an attacker-chosen ``Location``. This mirrors the
    test-connection path in ``routes/calendar_routes.py``, which already sets
    ``follow_redirects=False``.

    DAVClient exposes no per-request redirect flag, so we set it on the session
    after construction (the session is created in ``__init__``).

    ``token``, when given, authenticates via ``Authorization: Bearer`` instead
    of Basic Auth — Google's CalDAV endpoint requires OAuth 2.0 and rejects a
    username/password pair outright.
    """
    import caldav

    if token:
        client = caldav.DAVClient(url=url, headers={"Authorization": f"Bearer {token}"})
    else:
        client = caldav.DAVClient(url=url, username=username, password=password)
    # Unconditional: a redirect-disable that only sometimes applies is not a
    # control. The session exists right after __init__ on every real client;
    # test_build_dav_client_disables_redirects asserts it against installed
    # caldav in CI.
    client.session.max_redirects = 0
    return client


def _should_prune_window(seen_uids: set, parse_failed: bool) -> bool:
    """Whether the post-sync prune of vanished CalDAV events is safe to run.

    The prune deletes local ``origin=="caldav"`` rows in the window whose UID the
    server did not just return. Any parse failure (total or partial) makes
    ``seen_uids`` an incomplete view of the server, so pruning against it can
    delete events that still exist upstream but could not be read: a total
    failure wipes the whole window, a partial failure deletes just the
    unreadable ones. Only prune on a clean read. An empty ``seen_uids`` after a
    clean read is a genuinely empty window, which is safe to prune.
    """
    return not parse_failed


def _sync_blocking(owner: str, url: str, username: str, password: str, account_id: str = "", token: str = "") -> dict:
    """The actual sync — synchronous, intended to run in a threadpool.
    Returns counts: {calendars, events, deleted, errors}."""
    # Lazy imports so a missing `caldav` dep doesn't break app startup —
    # the integrations form still works, sync just no-ops with an error.
    from caldav.lib.error import AuthorizationError, NotFoundError
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    from routes.calendar_routes import _ensure_positive_duration

    result = {"calendars": 0, "events": 0, "deleted": 0, "skipped_uid_conflicts": 0, "errors": []}

    client = _build_dav_client(url, username, password, token=token)
    try:
        # Discovery: try principal → calendars first; if the server doesn't
        # support discovery (or the URL points directly at a calendar), fall
        # back to treating the URL as a single calendar.
        calendars = []
        google_collection = _google_calendar_collection_url(url)
        if google_collection:
            # Google's OAuth URL is already a concrete collection (or maps to
            # one). Asking python-caldav for current-user-principal first emits
            # warnings and spends an extra PROPFIND on every sync/write.
            calendars = [client.calendar(url=google_collection)]
        else:
            try:
                principal = client.principal()
                calendars = principal.calendars()
            except (AuthorizationError, NotFoundError) as e:
                result["errors"].append(f"Discovery failed: {e}")
                return result          # outer finally will call client.close()
            except Exception as e:
                logger.info(f"CalDAV principal discovery failed, trying URL as calendar: {e}")
                try:
                    calendars = [_open_url_as_calendar(client, url)]
                except Exception as e2:
                    result["errors"].append(f"Could not open URL as calendar: {e2}")
                    return result      # outer finally will call client.close()

        if not calendars:
            try:
                calendars = [_open_url_as_calendar(client, url)]
            except Exception as e:
                result["errors"].append(f"No calendars and URL fallback failed: {e}")
                return result      # outer finally will call client.close()

        start = datetime.utcnow() - timedelta(days=_LOOKBACK_DAYS)
        end = datetime.utcnow() + timedelta(days=_LOOKAHEAD_DAYS)

        db = SessionLocal()        # if this raises, outer finally still calls client.close()
        try:
            for remote_cal in calendars:
                try:
                    remote_url = str(remote_cal.url)
                    cal_id = _stable_cal_id(remote_url, owner=owner, account_id=account_id)
                    display_name = (remote_cal.name or "").strip() or "CalDAV"

                    local_cal = db.query(CalendarCal).filter(
                        CalendarCal.id == cal_id,
                        CalendarCal.owner == owner,
                    ).first()
                    if not local_cal:
                        local_cal = CalendarCal(
                            id=cal_id,
                            owner=owner,
                            name=display_name,
                            color="#5b8abf",
                            source="caldav",
                            account_id=account_id or None,
                            caldav_base_url=remote_url,
                        )
                        db.add(local_cal)
                        db.commit()
                    else:
                        # Refresh display name and stamp CalDAV metadata if missing.
                        changed = False
                        if local_cal.name != display_name:
                            local_cal.name = display_name
                            changed = True
                        if account_id and not local_cal.account_id:
                            local_cal.account_id = account_id
                            changed = True
                        if local_cal.caldav_base_url != remote_url:
                            local_cal.caldav_base_url = remote_url
                            changed = True
                        if changed:
                            db.commit()
                    result["calendars"] += 1

                    # Fetch events in window. `date_search` returns CalendarObject
                    # resources; each may contain one VEVENT (most servers) or
                    # several (rare).
                    from icalendar import Calendar as iCal

                    _skipped_at_start = result["skipped_uid_conflicts"]
                    seen_uids = set()
                    # Track events added to the session but not yet committed so
                    # duplicate UIDs within the same batch are updated, not re-inserted
                    # (which would violates the UNIQUE constraint on commit).
                    pending: dict = {}
                    parse_failed = False
                    try:
                        objs = remote_cal.date_search(start=start, end=end, expand=False)
                    except Exception as e:
                        result["errors"].append(f"{display_name}: date_search failed ({e})")
                        continue

                    for obj in objs:
                        try:
                            ical = iCal.from_ical(obj.data)
                        except Exception as e:
                            result["errors"].append(f"{display_name}: parse failed ({e})")
                            parse_failed = True
                            continue

                        for comp in ical.walk():
                            if comp.name != "VEVENT":
                                continue
                            uid_val = str(comp.get("uid", "")) or str(uuid.uuid4())
                            seen_uids.add(uid_val)

                            dtstart_p = comp.get("dtstart")
                            if not dtstart_p:
                                continue
                            start_dt, all_day = _to_utc_naive(dtstart_p.dt)

                            dtend_p = comp.get("dtend")
                            if dtend_p:
                                end_dt, _ = _to_utc_naive(dtend_p.dt)
                            elif all_day:
                                end_dt = start_dt + timedelta(days=1)
                            else:
                                end_dt = start_dt + timedelta(hours=1)
                            # A synced event with DTEND <= DTSTART (e.g. a single-day
                            # all-day event whose source wrote DTEND equal to DTSTART)
                            # would be stored zero-duration and silently dropped by the
                            # list_events overlap filter. Clamp to a positive span.
                            end_dt = _ensure_positive_duration(start_dt, end_dt, all_day)

                            # is_utc reflects whether the source carried a TZ
                            # we converted from. All-day = no TZ semantics.
                            row_is_utc = (
                                not all_day
                                and isinstance(dtstart_p.dt, datetime)
                                and dtstart_p.dt.tzinfo is not None
                            )

                            summary = str(comp.get("summary", ""))
                            description = str(comp.get("description", ""))
                            location = str(comp.get("location", ""))
                            rrule = (
                                comp.get("rrule").to_ical().decode()
                                if comp.get("rrule")
                                else ""
                            )

                            existing = _find_existing_event(
                                db, pending, uid_val, local_cal.id, owner,
                            )
                            if existing:
                                if existing.caldav_sync_pending in {"create", "update"}:
                                    result["events"] += 1
                                    continue
                                existing.calendar_id = local_cal.id
                                existing.summary = summary
                                existing.description = description
                                existing.location = location
                                existing.dtstart = start_dt
                                existing.dtend = end_dt
                                existing.all_day = all_day
                                existing.is_utc = row_is_utc
                                existing.rrule = rrule
                                existing.origin = "caldav"
                                existing.remote_href = str(getattr(obj, "url", "") or "") or None
                                existing.remote_etag = _event_etag(obj) or None
                                existing.caldav_sync_pending = None
                            else:
                                new_ev = CalendarEvent(
                                    uid=uid_val,
                                    calendar_id=local_cal.id,
                                    summary=summary,
                                    description=description,
                                    location=location,
                                    dtstart=start_dt,
                                    dtend=end_dt,
                                    all_day=all_day,
                                    is_utc=row_is_utc,
                                    rrule=rrule,
                                    origin="caldav",
                                    remote_href=str(getattr(obj, "url", "") or "") or None,
                                    remote_etag=_event_etag(obj) or None,
                                )
                                # Isolate the insert: without a savepoint a single
                                # IntegrityError surfaces at the batch commit below
                                # and rolls back every event parsed for this
                                # calendar, so one bad uid cost the whole sync.
                                sp = db.begin_nested()
                                try:
                                    db.add(new_ev)
                                    db.flush()
                                except IntegrityError:
                                    sp.rollback()
                                    result["skipped_uid_conflicts"] += 1
                                    continue
                                else:
                                    sp.commit()
                                    pending[uid_val] = new_ev
                            result["events"] += 1
                    _skipped_here = result["skipped_uid_conflicts"] - _skipped_at_start
                    if _skipped_here:
                        logger.warning(
                            "CalDAV sync %s: skipped %d event(s) whose uid is already "
                            "held by another calendar; the rest of this calendar synced "
                            "normally.", display_name, _skipped_here,
                        )
                    db.commit()

                    # Prune locally-cached CalDAV events that vanished
                    # upstream (only within our sync window — events outside
                    # the window aren't in `objs`, so we'd false-delete them).
                    # Only rows we previously pulled from the server (origin=="caldav")
                    # are prunable; locally-created events (agent / email triage / a
                    # UI event whose write-back failed) carry origin NULL and must
                    # never be deleted just because the server didn't return them.
                    # Skip the prune on any parse failure: seen_uids is then an
                    # incomplete view of the server, so pruning against it would
                    # delete events that still exist upstream but could not be read
                    # (the empty-seen_uids case wipes the whole window; a partial
                    # failure deletes just the unreadable rows).
                    if _should_prune_window(seen_uids, parse_failed):
                        stale = db.query(CalendarEvent).filter(
                            CalendarEvent.calendar_id == local_cal.id,
                            CalendarEvent.origin == "caldav",
                            CalendarEvent.dtstart >= start,
                            CalendarEvent.dtstart <= end,
                            CalendarEvent.remote_href.isnot(None),
                            CalendarEvent.caldav_sync_pending.is_(None),
                            ~CalendarEvent.uid.in_(seen_uids) if seen_uids else CalendarEvent.uid.isnot(None),
                        ).all()
                        for ev in stale:
                            db.delete(ev)
                        result["deleted"] += len(stale)
                        db.commit()
                except Exception as e:
                    logger.exception("CalDAV sync failed for one calendar")
                    result["errors"].append(str(e)[:200])
                    db.rollback()
        finally:
            db.close()             # NOT client.close() here anymore

        return result
    finally:
        client.close()             # always called


def _event_payload(ev) -> dict:
    return {
        "uid": ev.uid,
        "summary": ev.summary,
        "description": ev.description,
        "location": ev.location,
        "dtstart": ev.dtstart,
        "dtend": ev.dtend,
        "all_day": ev.all_day,
        "is_utc": ev.is_utc,
        "rrule": ev.rrule or "",
        "recurrence_exdates": json.loads(ev.recurrence_exdates or "[]") if getattr(ev, "recurrence_exdates", "") else [],
    }


def _load_event_for_writeback(owner: str, uid: str) -> tuple[str, str, dict] | None:
    from core.database import CalendarCal, CalendarEvent, SessionLocal

    db = SessionLocal()
    try:
        ev = (
            db.query(CalendarEvent)
            .join(CalendarCal)
            .filter(CalendarEvent.uid == uid, CalendarCal.owner == owner)
            .first()
        )
        if not ev or not ev.calendar or ev.calendar.source != "caldav":
            return None
        return ev.calendar.source, ev.calendar.id, _event_payload(ev)
    finally:
        db.close()


def _load_delete_for_writeback(owner: str, uid: str) -> tuple[str, str, dict] | None:
    from core.database import CalendarCal, CalendarDeletedEvent, CalendarEvent, SessionLocal

    db = SessionLocal()
    try:
        tombstone = db.query(CalendarDeletedEvent).filter(
            CalendarDeletedEvent.uid == uid,
            CalendarDeletedEvent.owner == owner,
        ).first()
        if tombstone:
            return "caldav", tombstone.calendar_id, {"uid": uid}

        ev = (
            db.query(CalendarEvent)
            .join(CalendarCal)
            .filter(CalendarEvent.uid == uid, CalendarCal.owner == owner)
            .first()
        )
        if not ev or not ev.calendar or ev.calendar.source != "caldav":
            return None
        return ev.calendar.source, ev.calendar.id, {"uid": uid}
    finally:
        db.close()


def _pending_writeback_uids(owner: str) -> tuple[list[str], list[str]]:
    from core.database import CalendarCal, CalendarDeletedEvent, CalendarEvent, SessionLocal

    db = SessionLocal()
    try:
        rows = (
            db.query(CalendarEvent.uid)
            .join(CalendarCal)
            .filter(
                CalendarCal.owner == owner,
                CalendarCal.source == "caldav",
                CalendarEvent.status != "cancelled",
                (
                    (CalendarEvent.caldav_sync_pending.isnot(None))
                    | (CalendarEvent.remote_href.is_(None))
                ),
            )
            .all()
        )
        delete_rows = (
            db.query(CalendarDeletedEvent.uid)
            .filter(CalendarDeletedEvent.owner == owner)
            .all()
        )
        return [row[0] for row in rows], [row[0] for row in delete_rows]
    finally:
        db.close()


def _load_caldav_accounts(owner: str) -> list:
    """Return the list of CalDAV accounts for *owner*, auto-migrating the legacy
    single-account ``caldav`` key to the new ``caldav_accounts`` list on first call.

    The save step is best-effort: if ``_save_for_user`` is unavailable (e.g. in a
    test with a minimal prefs mock) the migrated accounts are still returned; the
    next real call will just re-run the cheap migration again.
    """
    import uuid as _uuid
    from routes.prefs_routes import _load_for_user

    prefs = _load_for_user(owner) or {}
    if "caldav_accounts" in prefs:
        return list(prefs["caldav_accounts"] or [])
    # Migrate legacy single-account config to the list format.
    legacy = prefs.get("caldav", {}) or {}
    if legacy.get("url"):
        accounts = [{
            "id": str(_uuid.uuid4()),
            "label": "CalDAV",
            "url": legacy["url"],
            "username": legacy.get("username", ""),
            "password": legacy.get("password", ""),
        }]
        prefs["caldav_accounts"] = accounts
        prefs.pop("caldav", None)
        try:
            from routes.prefs_routes import _save_for_user
            _save_for_user(owner, prefs)
        except (ImportError, AttributeError):
            pass  # best-effort; next call re-migrates from the still-present legacy key
        return accounts
    return []


def _save_caldav_accounts(owner: str, accounts: list) -> None:
    """Persist the full CalDAV account list for *owner*. Single source of
    truth for the write side — routes/calendar_routes.py delegates here too."""
    from routes.prefs_routes import _load_for_user, _save_for_user
    prefs = _load_for_user(owner) or {}
    prefs["caldav_accounts"] = accounts
    prefs.pop("caldav", None)
    _save_for_user(owner, prefs)


# --- Google OAuth token refresh -------------------------------------------
#
# Token-refresh outcomes, in the order callers care about. The distinction is
# not cosmetic: a terminal failure will never fix itself, so it has to reach
# the user, while a transient one must stay a quiet retry or every flaky
# network minute turns into a "reconnect your calendar" scare.
TOKEN_OK = "ok"
TOKEN_UNCONFIGURED = "unconfigured"   # the server itself has no OAuth client set up
TOKEN_TERMINAL = "terminal"           # grant is dead; only re-authorization fixes it
TOKEN_TRANSIENT = "transient"         # blip; the next sync may well succeed

# RFC 6749 §5.2 error codes that mean the stored *grant* is gone, not that the
# request was unlucky. Google answers a revoked or expired refresh token with
# ``invalid_grant``; the others show up when the OAuth client itself has been
# deleted or had the scope withdrawn. All of them survive a retry, so we stop
# retrying and tell the user to reconnect.
GOOGLE_TERMINAL_TOKEN_ERRORS = frozenset({
    "invalid_grant",
    "invalid_client",
    "unauthorized_client",
    "invalid_scope",
})

# User-facing text for each terminal-ish case. Kept here so the sync response,
# the write-back path and the /test probe all say the same thing.
GOOGLE_REAUTH_REQUIRED = (
    "Google Calendar access has expired or been revoked — reconnect the account in Settings"
)
GOOGLE_REFRESH_TRANSIENT = (
    "Google Calendar token refresh failed temporarily — will retry on the next sync"
)
GOOGLE_OAUTH_UNCONFIGURED = (
    "Google Calendar OAuth is not configured on this server — set "
    "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET"
)


def google_token_error_message(status: str) -> str:
    """User-facing text for a non-OK ``TOKEN_*`` status.

    Single source of truth for the three call sites that report a missing
    Google token — the pull (sync_caldav), the push (caldav_writeback) and the
    /test probe — so they cannot drift into saying different things about the
    same failure.
    """
    if status == TOKEN_TRANSIENT:
        return GOOGLE_REFRESH_TRANSIENT
    if status == TOKEN_UNCONFIGURED:
        return GOOGLE_OAUTH_UNCONFIGURED
    return GOOGLE_REAUTH_REQUIRED


def _classify_google_token_failure(resp) -> tuple[str, str]:
    """Classify a failed Google token-endpoint response as terminal or transient.

    Returns ``(TOKEN_TERMINAL | TOKEN_TRANSIENT, oauth_error_code)``.

    ``resp`` is None when the request never produced a response at all (DNS,
    TLS, connect timeout) — always transient.

    We read the OAuth error code out of the JSON body rather than trusting the
    status alone, because Google answers *both* "your refresh token is dead"
    and "you sent a malformed request" with a bare 400. The body can also be
    missing or not JSON at all (a proxy's HTML error page), so we fall back to
    the status: this endpoint only answers 400/401 over credentials, never as a
    transient blip, while 429/5xx are retryable by definition.

    Only the short error *code* is returned — never ``error_description``,
    which is free-form upstream text, and never any part of the request, which
    carries the client secret and the refresh token.
    """
    if resp is None:
        return TOKEN_TRANSIENT, ""
    status = getattr(resp, "status_code", 0) or 0
    code = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error")
            # Google sends a bare string here; some proxies wrap it in an object.
            if isinstance(err, str):
                code = err.strip().lower()
            elif isinstance(err, dict):
                code = str(err.get("status") or err.get("code") or "").strip().lower()
    except Exception:
        code = ""
    if code:
        return (TOKEN_TERMINAL if code in GOOGLE_TERMINAL_TOKEN_ERRORS else TOKEN_TRANSIENT), code
    # No usable body: 400/401 from a token endpoint is a credential verdict,
    # anything else (429, 5xx, a stray 3xx) is worth retrying.
    return (TOKEN_TERMINAL if status in (400, 401) else TOKEN_TRANSIENT), ""


def _mark_google_caldav_account(owner: str, account_id: str, *, needs_reconnect: bool) -> None:
    """Persist (or clear) the "this Google account needs re-authorization" flag
    on the stored caldav_accounts entry.

    The sync response only tells whoever happened to call /sync. The flag is
    what makes the dead account visible afterwards: routes/calendar_routes.py's
    account listing folds it into ``needs_reconnect``, which the Settings card
    already renders as "⚠ Needs reconnecting". Without it a revoked account
    keeps advertising "✓ Connected via Google OAuth" forever.

    Best-effort: prefs may be unwritable (read-only config, minimal test mock)
    and a failure here must never sink an otherwise working sync.
    """
    try:
        accounts = _load_caldav_accounts(owner)
        changed = False
        for acc in accounts:
            if acc.get("id") != account_id:
                continue
            if needs_reconnect:
                if not acc.get("oauth_needs_reconnect"):
                    acc["oauth_needs_reconnect"] = True
                    changed = True
            elif acc.pop("oauth_needs_reconnect", None):
                changed = True
            break
        if changed:
            _save_caldav_accounts(owner, accounts)
    except Exception:
        logger.debug("Could not persist reconnect flag for account %s", account_id, exc_info=True)


def _refresh_google_caldav_token_status(
    owner: str, account_id: str, refresh_token: str
) -> tuple[str | None, str]:
    """Exchange a stored refresh token for a new Google access token and
    persist it onto the matching caldav_accounts entry. Mirrors
    routes/email_helpers.py's ``_refresh_google_token`` but reads/writes the
    prefs-stored caldav account list instead of the EmailAccount table.

    Returns ``(access_token, status)`` where status is one of the ``TOKEN_*``
    constants. Callers use the status to decide whether to nag the user
    (terminal) or stay quiet and retry (transient).
    """
    import time as _time

    import httpx

    from src.secret_storage import encrypt as _enc

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        # Server-side misconfiguration, not the user's credentials — don't tell
        # them to reconnect an account that is fine.
        logger.warning("Google Calendar refresh skipped: OAuth client is not configured")
        return None, TOKEN_UNCONFIGURED

    resp = None
    try:
        resp = httpx.post("https://oauth2.googleapis.com/token", data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        access_token = data["access_token"]
    except Exception:
        status, code = _classify_google_token_failure(resp)
        # Never log the token, the refresh token or the client secret — the
        # account id and the short OAuth error code are enough to debug with.
        logger.warning(
            "Google Calendar token refresh failed for account %s (%s%s)",
            account_id, status, f": {code}" if code else "",
        )
        if status == TOKEN_TERMINAL:
            _mark_google_caldav_account(owner, account_id, needs_reconnect=True)
        return None, status

    expiry = str(int(_time.time()) + data.get("expires_in", 3600))
    accounts = _load_caldav_accounts(owner)
    found = False
    for acc in accounts:
        if acc.get("id") == account_id:
            acc["oauth_access_token"] = _enc(access_token)
            acc["oauth_token_expiry"] = expiry
            # A working refresh clears any earlier terminal verdict — e.g. the
            # user reconnected, or Google's 400 was a one-off after all.
            acc.pop("oauth_needs_reconnect", None)
            found = True
            break
    if found:
        _save_caldav_accounts(owner, accounts)
    return access_token, TOKEN_OK


def _refresh_google_caldav_token(owner: str, account_id: str, refresh_token: str) -> str | None:
    """Back-compatible wrapper: the access token, or None on any failure."""
    return _refresh_google_caldav_token_status(owner, account_id, refresh_token)[0]


def _google_caldav_token_status(owner: str, acc: dict) -> tuple[str | None, str]:
    """Return ``(access_token, status)`` for a Google-OAuth CalDAV account,
    refreshing via the stored refresh token if the cached one is missing or
    expired.

    The status is what lets callers tell "reconnect this account" apart from
    "the token endpoint was briefly unhappy" — see the ``TOKEN_*`` constants.
    """
    import time as _time

    from src.secret_storage import decrypt as _dec

    try:
        access_token = _dec(acc.get("oauth_access_token") or "")
    except Exception:
        access_token = ""
    expiry_str = acc.get("oauth_token_expiry") or ""
    if access_token and expiry_str:
        try:
            if int(expiry_str) - 60 > _time.time():
                return access_token, TOKEN_OK
        except (ValueError, TypeError):
            pass
    try:
        refresh_token = _dec(acc.get("oauth_refresh_token") or "")
    except Exception:
        refresh_token = ""
    if not refresh_token:
        # Never connected, or the token was cleared out. Same remedy as a
        # revoked grant, so it is terminal too.
        return None, TOKEN_TERMINAL
    if acc.get("oauth_needs_reconnect"):
        # A previous refresh got an invalid_grant. Re-asking Google produces the
        # same 400 every sync, so short-circuit until the user reconnects.
        return None, TOKEN_TERMINAL
    return _refresh_google_caldav_token_status(owner, acc.get("id") or "", refresh_token)


def _get_valid_google_caldav_token(owner: str, acc: dict) -> str | None:
    """Return a valid Google OAuth access token for a CalDAV account, or None if
    the account was never connected via OAuth or the refresh failed. Callers
    that need to tell those apart use ``_google_caldav_token_status``."""
    return _google_caldav_token_status(owner, acc)[0]


async def sync_caldav(owner: str) -> dict:
    """Pull CalDAV state into local DB for `owner` across all configured accounts.

    Returns aggregated counts, per-account error strings, and ``auth_errors`` —
    the subset of failures that no amount of retrying will clear, as structured
    ``{account_id, label, provider, message}`` entries. Callers that can only
    afford to interrupt the user once (the calendar's background sync) watch
    ``auth_errors``; everything else reads ``errors``.
    """
    from src.secret_storage import decrypt

    accounts = _load_caldav_accounts(owner)
    if not accounts:
        return {
            "calendars": 0, "events": 0, "deleted": 0,
            "errors": ["CalDAV is not configured"], "auth_errors": [],
        }

    totals: dict = {"calendars": 0, "events": 0, "deleted": 0, "errors": [], "auth_errors": []}
    for acc in accounts:
        url = (acc.get("url") or "").strip()
        account_id = acc.get("id") or ""
        # The label is echoed straight back to the UI, and a CalDAV URL can
        # carry credentials in its userinfo (https://user:pass@host), so the
        # URL fallback goes through redact_url first.
        label = acc.get("label") or redact_url(url) or account_id
        is_google_oauth = acc.get("oauth_provider") == "google"

        if is_google_oauth:
            token, token_status = _google_caldav_token_status(owner, acc)
            if not token:
                # A dead grant and a hiccup at the token endpoint both land
                # here, but they need opposite handling: only the first has to
                # reach the user, and only it goes into auth_errors. (A server
                # with no OAuth client at all is the operator's problem —
                # reconnecting would fail the same way, so no prompt for it.)
                message = google_token_error_message(token_status)
                totals["errors"].append(f"{label}: {message}")
                if token_status == TOKEN_TERMINAL:
                    totals["auth_errors"].append({
                        "account_id": account_id,
                        "label": label,
                        "provider": "google",
                        "message": message,
                    })
                continue
            try:
                url = validate_caldav_url(url)
                result = await asyncio.to_thread(
                    _sync_blocking, owner, url, "", "", account_id, token
                )
            except ValueError as e:
                result = {"calendars": 0, "events": 0, "deleted": 0, "errors": [str(e)]}
            except Exception as e:
                logger.exception("CalDAV sync raised for account %s", label)
                result = {"calendars": 0, "events": 0, "deleted": 0, "errors": [str(e)[:200]]}
        else:
            user = (acc.get("username") or "").strip()
            pw = acc.get("password") or ""
            try:
                pw = decrypt(pw)
            except Exception:
                pass
            if not (url and user and pw):
                totals["errors"].append(f"{label}: missing URL, username, or password")
                continue
            try:
                url = validate_caldav_url(url)
                if is_google_caldav_url(url):
                    totals["errors"].append(f"{label}: {GOOGLE_CALDAV_OAUTH_REQUIRED}")
                    continue
                result = await asyncio.to_thread(_sync_blocking, owner, url, user, pw, account_id)
            except ValueError as e:
                result = {"calendars": 0, "events": 0, "deleted": 0, "errors": [str(e)]}
            except Exception as e:
                logger.exception("CalDAV sync raised for account %s", label)
                result = {"calendars": 0, "events": 0, "deleted": 0, "errors": [str(e)[:200]]}

        totals["calendars"] += result.get("calendars", 0)
        totals["events"] += result.get("events", 0)
        totals["deleted"] += result.get("deleted", 0)
        for err in result.get("errors", []):
            totals["errors"].append(f"{label}: {err}")
    return totals


async def push_event_create(owner: str, uid: str) -> dict:
    loaded = _load_event_for_writeback(owner, uid)
    if not loaded:
        return {"ok": True, "skipped": True}
    source, calendar_id, payload = loaded
    from src.caldav_writeback import writeback_event
    return await writeback_event(owner, source, calendar_id, payload)


async def push_event_update(owner: str, uid: str) -> dict:
    return await push_event_create(owner, uid)


async def push_event_delete(owner: str, uid: str) -> dict:
    loaded = _load_delete_for_writeback(owner, uid)
    if not loaded:
        return {"ok": True, "skipped": True}
    source, calendar_id, payload = loaded
    from src.caldav_writeback import writeback_event
    return await writeback_event(owner, source, calendar_id, payload, delete=True)


async def push_pending_events(owner: str) -> dict:
    result: dict = {"events": 0, "errors": [], "auth_errors": []}
    uids, delete_uids = _pending_writeback_uids(owner)

    def _note_auth_error(out: dict) -> None:
        """Hoist a write-back's terminal-credential marker onto the aggregate,
        de-duplicated by account: one dead account can fail every queued event
        and the user only needs telling once."""
        auth = (out or {}).get("auth_error")
        if auth and not any(
            e.get("account_id") == auth.get("account_id") for e in result["auth_errors"]
        ):
            result["auth_errors"].append(auth)

    for event_uid in uids:
        try:
            out = await push_event_update(owner, event_uid)
            if out.get("ok"):
                result["events"] += 1
            elif not out.get("skipped"):
                result["errors"].append(f"{event_uid}: {str(out.get('error') or out)[:160]}")
                _note_auth_error(out)
        except Exception as e:
            logger.warning("CalDAV pending push failed for uid=%s: %s", event_uid, e)
            result["errors"].append(f"{event_uid}: {str(e)[:160]}")
    for event_uid in delete_uids:
        try:
            out = await push_event_delete(owner, event_uid)
            if out.get("ok"):
                result["events"] += 1
            elif not out.get("skipped"):
                result["errors"].append(f"{event_uid}: {str(out.get('error') or out)[:160]}")
                _note_auth_error(out)
        except Exception as e:
            logger.warning("CalDAV pending delete failed for uid=%s: %s", event_uid, e)
            result["errors"].append(f"{event_uid}: {str(e)[:160]}")
    return result


async def sync_caldav_direction(owner: str, direction: str = "pull") -> dict:
    direction = (direction or "pull").strip().lower()
    if direction == "pull":
        return await sync_caldav(owner)
    if direction == "push":
        return await push_pending_events(owner)
    if direction == "both":
        pushed = await push_pending_events(owner)
        pulled = await sync_caldav(owner)
        # The nested {"push": ..., "pull": ...} shape this used to return had no
        # top-level "errors" key, so routes/calendar_routes.py's
        # `caldav_result.get("errors", [])` silently read an empty list and the
        # endpoint reported a clean 200 over a failed sync. Flatten to the same
        # contract the other directions use and keep the sub-results alongside
        # it for callers that want the breakdown.
        return {
            "calendars": pulled.get("calendars", 0),
            "events": pushed.get("events", 0) + pulled.get("events", 0),
            "deleted": pulled.get("deleted", 0),
            "errors": [
                *(f"push: {err}" for err in pushed.get("errors", [])),
                *pulled.get("errors", []),
            ],
            "auth_errors": [*pushed.get("auth_errors", []), *pulled.get("auth_errors", [])],
            "push": pushed,
            "pull": pulled,
        }
    return {
        "calendars": 0,
        "events": 0,
        "deleted": 0,
        "errors": [f"Unsupported CalDAV sync direction: {direction}"],
        "auth_errors": [],
    }

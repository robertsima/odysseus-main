"""Connection occupancy diagnostics without queries, credentials or SQL text."""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)
_APP_ROOT = str(Path(__file__).resolve().parent.parent)


def _checkout_origin() -> str:
    """Record a code location, never frame locals or query parameters."""
    frame = sys._getframe(1)
    try:
        for _ in range(40):
            if frame is None:
                break
            filename = frame.f_code.co_filename
            if filename.startswith(_APP_ROOT) and filename != __file__:
                # Prefer the consumer to the shared context-manager helper.
                if not filename.endswith(("core/database.py", "core\\database.py")):
                    return f"{Path(filename).name}:{frame.f_lineno}:{frame.f_code.co_name}"
            frame = frame.f_back
    finally:
        del frame
    return "unknown"


class PoolDiagnostics:
    def __init__(self, engine):
        self.engine = engine
        self._active = {}
        self._lock = threading.Lock()
        event.listen(engine, "checkout", self._checkout)
        event.listen(engine, "checkin", self._checkin)
        event.listen(engine, "invalidate", self._invalidate)

    def _checkout(self, connection, record, proxy):
        started, origin = time.monotonic(), _checkout_origin()
        with self._lock:
            self._active[id(record)] = (started, origin)

    def _checkin(self, connection, record):
        with self._lock:
            entry = self._active.pop(id(record), None)
        if entry and (elapsed := time.monotonic() - entry[0]) >= 5:
            logger.warning("[db-pool] long checkout seconds=%.2f origin=%s", elapsed, entry[1])

    def _invalidate(self, connection, record, exception):
        self._checkin(connection, record)

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            rows = list(self._active.values())
        rows.sort(key=lambda row: row[0])
        return {
            "pool": self.engine.pool.status(),
            "active_count": len(rows),
            "oldest_holders": [
                {"seconds": round(max(0, now - started), 2), "origin": origin}
                for started, origin in rows[:8]
            ],
        }


def install_database_error_handler(app, diagnostics):
    async def database_busy(request, exc):
        # No str(exc): a driver error can contain connection details or SQL.
        logger.error("[db-pool] checkout timed out occupancy=%s", diagnostics.snapshot())
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "5"},
            content={
                "error": "DATABASE_BUSY",
                "message": "The database is temporarily busy. Please retry shortly.",
            },
        )

    app.add_exception_handler(PoolTimeout, database_busy)

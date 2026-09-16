"""Bounded, content-free in-memory route latency aggregation."""

import re
import threading
from collections import deque
from typing import Dict, List

_MAX_SAMPLES_PER_ROUTE = 200
_MAX_ROUTES = 200
_lock = threading.Lock()
_samples: Dict[str, deque] = {}
_counts: Dict[str, int] = {}
_ID_SEGMENT_RE = re.compile(r"^(?:[0-9a-fA-F-]{8,}|\d+|mcp__[\w.-]+__[\w.-]+)$")


def normalize_path(path: str) -> str:
    parts = (path or "/").split("/")
    normalized = ["{id}" if _ID_SEGMENT_RE.match(part) else part for part in parts]
    return "/".join(normalized) or "/"


def record(method: str, path: str, elapsed_seconds: float) -> None:
    key = f"{method.upper()} {normalize_path(path)}"
    with _lock:
        bucket = _samples.get(key)
        if bucket is None:
            if len(_samples) >= _MAX_ROUTES:
                return
            bucket = deque(maxlen=_MAX_SAMPLES_PER_ROUTE)
            _samples[key] = bucket
        bucket.append(elapsed_seconds)
        _counts[key] = _counts.get(key, 0) + 1


def _percentile(sorted_values: List[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(round(pct * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def stats() -> List[Dict]:
    with _lock:
        rows = []
        for key, bucket in _samples.items():
            values = sorted(bucket)
            method, _, path = key.partition(" ")
            rows.append({
                "method": method,
                "path": path,
                "count": _counts.get(key, len(values)),
                "sampled": len(values),
                "avg_ms": round((sum(values) / len(values)) * 1000, 1) if values else 0.0,
                "p95_ms": round(_percentile(values, 0.95) * 1000, 1),
                "max_ms": round((values[-1] if values else 0.0) * 1000, 1),
            })
    rows.sort(key=lambda row: row["p95_ms"], reverse=True)
    return rows


def reset() -> None:
    with _lock:
        _samples.clear()
        _counts.clear()

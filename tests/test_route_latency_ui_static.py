from pathlib import Path


_REPO = Path(__file__).resolve().parent.parent
_INDEX = (_REPO / "static" / "index.html").read_text(encoding="utf-8")
_ADMIN = (_REPO / "static" / "js" / "admin.js").read_text(encoding="utf-8")


def test_route_latency_card_is_present():
    assert 'id="adm-routeLatencyRefresh"' in _INDEX
    assert 'id="adm-routeLatencyList"' in _INDEX


def test_route_latency_loader_uses_diagnostics_endpoint_without_polling():
    start = _ADMIN.index("async function loadRouteLatency()")
    end = _ADMIN.index("/* ═", start)
    block = _ADMIN[start:end]
    assert "/api/diagnostics/route_latency" in block
    assert "setInterval" not in block

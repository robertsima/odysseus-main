import pytest

from src import route_latency


@pytest.fixture(autouse=True)
def _reset():
    route_latency.reset()
    yield
    route_latency.reset()


def test_normalize_path_collapses_id_shaped_segments():
    assert route_latency.normalize_path("/api/mcp/servers/a1b2c3d4/reconnect") == "/api/mcp/servers/{id}/reconnect"
    assert route_latency.normalize_path("/api/sessions/42/messages") == "/api/sessions/{id}/messages"


def test_record_aggregates_and_reports_stats():
    route_latency.record("POST", "/api/mcp/servers/aaa11111/reconnect", 0.1)
    route_latency.record("POST", "/api/mcp/servers/bbb22222/reconnect", 0.2)
    row = route_latency.stats()[0]
    assert row["path"] == "/api/mcp/servers/{id}/reconnect"
    assert row["count"] == 2
    assert row["max_ms"] == 200.0


def test_storage_is_bounded():
    for i in range(route_latency._MAX_ROUTES + 50):
        route_latency.record("GET", f"/api/distinct-path-{i}", 0.01)
    assert len(route_latency.stats()) <= route_latency._MAX_ROUTES


def test_sample_window_is_bounded():
    for _ in range(route_latency._MAX_SAMPLES_PER_ROUTE + 100):
        route_latency.record("GET", "/api/hot-route", 0.01)
    row = route_latency.stats()[0]
    assert row["sampled"] == route_latency._MAX_SAMPLES_PER_ROUTE
    assert row["count"] == route_latency._MAX_SAMPLES_PER_ROUTE + 100

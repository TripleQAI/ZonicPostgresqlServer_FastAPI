"""P3.10 — the streaming suite (ADR-006, doc 10 §10.5).

The two that matter most: connections are released even when the client walks
away mid-stream, and a mid-stream failure is reported in band rather than
truncating silently.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from pgfoundation import DataFoundation, QueryError, ResultSet
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver
from pgfoundation_api.app import create_app
from pgfoundation_api.settings import ServiceSettings

VIEW = {"name": "v_readings"}
STREAM = "/v1/datasources/main/view/stream"


def rows_responder(n: int = 5):
    def responder(spec):
        return ResultSet(rows=[{"id": i, "v": i * 2} for i in range(n)], rowcount=n)

    return responder


async def build(settings: ServiceSettings, *, driver=None):
    app_settings = load_settings({"datasources": [{"name": "main", "dsn": "postgresql://x/main"}]})
    foundation = await DataFoundation.from_settings(
        app_settings, driver=driver or FakeDriver(rows_responder())
    )
    return create_app(settings, foundation=foundation), foundation


@pytest.fixture
def settings() -> ServiceSettings:
    return ServiceSettings()


async def test_stream_returns_ndjson_by_default(settings):
    app, foundation = await build(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post(STREAM, json=VIEW)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")

        lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        assert lines == [{"id": i, "v": i * 2} for i in range(5)]
    await foundation.aclose()


async def test_stream_negotiates_sse(settings):
    app, foundation = await build(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post(STREAM, json=VIEW, headers={"accept": "text/event-stream"})
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text.startswith("data: ")
        assert response.text.count("data: ") == 5
    await foundation.aclose()


async def test_stream_releases_its_connection(settings):
    """A stream pins a connection; it must always come back."""
    driver = FakeDriver(rows_responder())
    app, foundation = await build(settings, driver=driver)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        await client.post(STREAM, json=VIEW)

    pool = next(iter(driver.pools.values()))
    assert pool.released == pool.acquired, f"leaked {pool.acquired - pool.released} connection(s)"
    await foundation.aclose()


async def test_mid_stream_error_is_reported_in_band(settings):
    """Headers are already sent, so the failure cannot change the status.

    A silently truncated stream is indistinguishable from a complete one — the
    final record is what tells the client otherwise (ADR-006 §4).
    """

    class Exploding(FakeDriver):
        def build_pool(self, dsn, policy):
            pool = super().build_pool(dsn, policy)
            original = pool.acquire

            async def acquire():
                conn = await original()

                async def stream(spec, *, itersize=1000):
                    yield {"id": 0}
                    yield {"id": 1}
                    raise QueryError("connection lost mid-scan")

                conn.stream = stream  # type: ignore[method-assign]
                return conn

            pool.acquire = acquire  # type: ignore[method-assign]
            return pool

    app, foundation = await build(settings, driver=Exploding())
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post(STREAM, json=VIEW)

        assert response.status_code == 200, "status was already sent before the failure"
        records = [json.loads(x) for x in response.text.splitlines() if x.strip()]
        assert records[:2] == [{"id": 0}, {"id": 1}]
        assert records[-1]["partial"] is True
        assert records[-1]["error"]["type"] == "QueryError"
    await foundation.aclose()


async def test_stream_is_not_buffered_by_intermediaries(settings):
    app, foundation = await build(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post(STREAM, json=VIEW)
        # Without these a proxy may buffer the whole response, destroying both
        # backpressure and liveness.
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
    await foundation.aclose()


async def test_stream_rejects_injection_like_the_unary_route(settings):
    """Streaming must not soften the identifier boundary."""
    app, foundation = await build(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post(STREAM, json={"name": "v; DROP TABLE x"})
    assert response.status_code == 400
    await foundation.aclose()


async def test_streaming_routes_cannot_be_batched(settings):
    """A stream pins a connection for the client's read duration (doc 06 §6.5)."""
    app, foundation = await build(settings)
    body = {"items": [{"id": "a", "datasource": "main", "op": "view_stream", "payload": VIEW}]}
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        out = (await client.post("/v1/batch", json=body)).json()

    assert out["error_count"] == 1
    assert "cannot be batched" in out["results"][0]["error"]["message"]
    await foundation.aclose()


async def test_unary_form_of_a_streamable_operation_is_still_batchable(settings):
    """Only the /stream route is excluded — `view` itself batches fine."""
    app, foundation = await build(settings)
    body = {"items": [{"id": "a", "datasource": "main", "op": "view", "payload": VIEW}]}
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        out = (await client.post("/v1/batch", json=body)).json()
    assert out["ok_count"] == 1
    await foundation.aclose()


# --- the four operations the Django docs designed but never built ---------- #


async def test_interval_returns_bucketed_rows(settings):
    app, foundation = await build(settings)
    payload = {
        "source": "readings",
        "time_column": "ts",
        "start": "2026-08-13T00:00:00Z",
        "end": "2026-08-14T00:00:00Z",
        "every": "15 minutes",
        "metrics": [{"column": "v", "func": "sum"}],
        "group_by": ["id"],
    }
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post("/v1/datasources/main/interval", json=payload)
    assert response.status_code == 200
    assert response.json()["row_count"] == 5
    await foundation.aclose()


async def test_interval_rejects_an_unknown_aggregate(settings):
    """`func` is whitelisted by the core compiler, not by the DTO."""
    app, foundation = await build(settings)
    payload = {
        "source": "readings",
        "time_column": "ts",
        "start": "a",
        "end": "b",
        "every": "1 hour",
        "metrics": [{"column": "v", "func": "drop_table"}],
    }
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post("/v1/datasources/main/interval", json=payload)
    assert response.status_code == 400
    await foundation.aclose()


async def test_fetch_since_returns_a_page_and_next_watermark(settings):
    app, foundation = await build(settings)
    payload = {
        "sql": "SELECT id, v FROM readings",
        "params": {},
        "cursor": {"order_by": ["id"]},
        "limit": 10,
    }
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        body = (await client.post("/v1/datasources/main/fetch-since", json=payload)).json()

    assert body["row_count"] == 5
    # The watermark resumes from the LAST row, so the next page has no overlap.
    assert body["next_watermark"]["after"] == [4]
    await foundation.aclose()


async def test_execute_many_reports_affected_rows(settings):
    app, foundation = await build(settings)
    payload = {
        "sql": "INSERT INTO readings (id) VALUES (%(id)s)",
        "params_seq": [{"id": 1}, {"id": 2}, {"id": 3}],
    }
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        body = (await client.post("/v1/datasources/main/execute-many", json=payload)).json()
    assert body["row_count"] == 3
    await foundation.aclose()


async def test_copy_ingests_rows(settings):
    driver = FakeDriver(rows_responder())
    app, foundation = await build(settings, driver=driver)
    payload = {"table": "readings", "rows": [[1, "a"], [2, "b"], [3, "c"]]}
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        body = (await client.post("/v1/datasources/main/copy", json=payload)).json()

    assert body["row_count"] == 3
    pool = next(iter(driver.pools.values()))
    assert pool.copied == [('"readings"', 3)]
    await foundation.aclose()


async def test_copy_rejects_an_unsafe_table_name(settings):
    app, foundation = await build(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post(
            "/v1/datasources/main/copy", json={"table": "t; DROP TABLE x", "rows": []}
        )
    assert response.status_code == 400
    await foundation.aclose()


# --- P3.8: the unary path is bounded --------------------------------------- #


async def test_oversize_unary_response_is_refused_with_413():
    """Materializing an unbounded result is how a worker OOMs (doc 06 §6.7)."""
    settings = ServiceSettings(api={"max_response_rows": 3})
    app, foundation = await build(settings, driver=FakeDriver(rows_responder(10)))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post("/v1/datasources/main/view", json=VIEW)

    assert response.status_code == 413
    body = response.json()
    assert "10 rows" in body["error"]
    # A streamable operation must name the route that has no such bound.
    assert "/stream" in body["error"]
    await foundation.aclose()


async def test_stream_route_has_no_row_bound():
    """The whole point of the 413: the stream is the way to get the big result."""
    settings = ServiceSettings(api={"max_response_rows": 3})
    app, foundation = await build(settings, driver=FakeDriver(rows_responder(10)))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.post(STREAM, json=VIEW)

    assert response.status_code == 200
    assert len([x for x in response.text.splitlines() if x.strip()]) == 10
    await foundation.aclose()

"""P2.13 — the concurrency suite (doc 10 §10.5).

These tests are what make requirement 4 a measured property rather than a claim.
Two in particular carry the phase: `test_parallel_batch_runs_concurrently` (the
fan-out is real) and `test_parallel_batch_respects_max_concurrency` (it is
bounded, so it is not a denial-of-service against our own database).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from pgfoundation import DataFoundation, ResultSet, TransientError
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver
from pgfoundation_api.app import create_app
from pgfoundation_api.settings import ServiceSettings

ITEM_DELAY = 0.05  # 50 ms per simulated statement


def slow_responder(delay: float = ITEM_DELAY):
    """A responder that costs wall-clock time, so concurrency is observable."""

    def responder(spec):
        time.sleep(0)  # keep it sync-cheap; the await below provides the delay
        return ResultSet(rows=[{"id": 1}], rowcount=1, elapsed_ms=delay * 1000)

    return responder


class SlowDriver(FakeDriver):
    """FakeDriver whose connections await a real delay per execute."""

    def __init__(self, delay: float = ITEM_DELAY, **kwargs):
        super().__init__(**kwargs)
        self.delay = delay

    def build_pool(self, dsn, policy):
        pool = super().build_pool(dsn, policy)
        delay = self.delay
        original = pool.acquire

        async def acquire():
            conn = await original()
            inner = conn.execute

            async def execute(spec, *, row_format="dict"):
                # Mirrors ConnectionPort.execute exactly — a double that drops a
                # keyword hides real breakage instead of surfacing it.
                await asyncio.sleep(delay)
                return await inner(spec, row_format=row_format)

            conn.execute = execute  # type: ignore[method-assign]
            return conn

        pool.acquire = acquire  # type: ignore[method-assign]
        return pool


async def build_app(settings: ServiceSettings, *, driver=None, names=("main",)):
    app_settings = load_settings(
        {"datasources": [{"name": n, "dsn": f"postgresql://x/{n}"} for n in names]}
    )
    foundation = await DataFoundation.from_settings(app_settings, driver=driver or FakeDriver())
    return create_app(settings, foundation=foundation), foundation


def items(n: int, datasource: str = "main", op: str = "view") -> list[dict]:
    return [
        {"id": f"i{k}", "datasource": datasource, "op": op, "payload": {"name": "v_orders"}}
        for k in range(n)
    ]


async def post_batch(app, body: dict):
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        return await client.post("/v1/batch", json=body)


# --------------------------------------------------------------------------- #
# The two that carry the phase
# --------------------------------------------------------------------------- #
async def test_parallel_batch_runs_concurrently():
    """10 items x 50 ms must finish in ~50-150 ms, not ~500 ms."""
    settings = ServiceSettings(
        batch={"max_concurrency": 16}, concurrency={"max_inflight_total": 32}
    )
    app, foundation = await build_app(settings, driver=SlowDriver())

    started = time.perf_counter()
    response = await post_batch(app, {"mode": "parallel", "items": items(10)})
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    body = response.json()
    assert body["ok_count"] == 10, body
    serial_time = 10 * ITEM_DELAY
    assert elapsed < serial_time * 0.5, (
        f"took {elapsed:.3f}s; serial would be ~{serial_time:.3f}s — fan-out is not concurrent"
    )
    await foundation.aclose()


async def test_parallel_batch_respects_max_concurrency():
    """Peak in-flight must never exceed the configured cap.

    An unbounded gather is a denial-of-service against our own database.
    """
    settings = ServiceSettings(batch={"max_concurrency": 3}, concurrency={"max_inflight_total": 32})
    app, foundation = await build_app(settings, driver=SlowDriver())

    response = await post_batch(app, {"mode": "parallel", "items": items(12)})
    assert response.status_code == 200
    assert app.state.limiter.peak_in_flight <= 3, (
        f"peak {app.state.limiter.peak_in_flight} exceeded the cap of 3"
    )
    await foundation.aclose()


async def test_client_cannot_raise_its_own_concurrency_ceiling():
    settings = ServiceSettings(batch={"max_concurrency": 2}, concurrency={"max_inflight_total": 32})
    app, foundation = await build_app(settings, driver=SlowDriver())
    await post_batch(app, {"mode": "parallel", "max_concurrency": 999, "items": items(8)})
    assert app.state.limiter.peak_in_flight <= 2
    await foundation.aclose()


# --------------------------------------------------------------------------- #
# Partial success, ordering, correlation
# --------------------------------------------------------------------------- #
async def test_partial_failure_does_not_cancel_siblings(settings):
    app, foundation = await build_app(settings)
    body = {
        "mode": "parallel",
        "items": [
            {"id": "good", "datasource": "main", "op": "view", "payload": {"name": "v_ok"}},
            {"id": "bad", "datasource": "main", "op": "view", "payload": {"name": "x; DROP"}},
            {"id": "also_good", "datasource": "main", "op": "view", "payload": {"name": "v2"}},
        ],
    }
    response = await post_batch(app, body)

    assert response.status_code == 200  # partial success is NOT an HTTP error
    out = response.json()
    assert out["ok_count"] == 2
    assert out["error_count"] == 1
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id["good"]["status"] == "ok"
    assert by_id["also_good"]["status"] == "ok"
    assert by_id["bad"]["status"] == "error"
    assert by_id["bad"]["error"]["type"] == "QueryError"
    await foundation.aclose()


async def test_results_are_returned_in_request_order(settings):
    app, foundation = await build_app(settings, driver=SlowDriver(delay=0.01))
    response = await post_batch(app, {"mode": "parallel", "items": items(6)})
    assert [r["id"] for r in response.json()["results"]] == [f"i{k}" for k in range(6)]
    await foundation.aclose()


async def test_unknown_operation_is_an_item_error_not_a_batch_error(settings):
    app, foundation = await build_app(settings)
    body = {"items": [{"id": "a", "datasource": "main", "op": "nope", "payload": {}}]}
    out = (await post_batch(app, body)).json()
    assert out["error_count"] == 1
    assert "unknown operation" in out["results"][0]["error"]["message"]
    await foundation.aclose()


# --------------------------------------------------------------------------- #
# Guardrails
# --------------------------------------------------------------------------- #
async def test_batch_exceeding_max_items_is_rejected_before_any_db_work(settings_small):
    app, foundation = await build_app(settings_small)
    response = await post_batch(app, {"items": items(50)})
    assert response.status_code == 400
    assert "maximum" in response.json()["error"]
    await foundation.aclose()


async def test_duplicate_item_ids_are_rejected(settings):
    app, foundation = await build_app(settings)
    body = {
        "items": [
            {"id": "same", "datasource": "main", "op": "view", "payload": {"name": "v"}},
            {"id": "same", "datasource": "main", "op": "view", "payload": {"name": "v"}},
        ]
    }
    response = await post_batch(app, body)
    assert response.status_code == 400
    assert "unique" in response.json()["error"]
    await foundation.aclose()


# --------------------------------------------------------------------------- #
# sequential mode
# --------------------------------------------------------------------------- #
async def test_sequential_uses_one_connection_and_pipelines(settings):
    driver = FakeDriver()
    app, foundation = await build_app(settings, driver=driver)
    response = await post_batch(app, {"mode": "sequential", "items": items(5)})

    assert response.status_code == 200
    assert response.json()["ok_count"] == 5
    pool = next(iter(driver.pools.values()))
    assert pool.acquired == 1, f"sequential used {pool.acquired} connections, expected 1"
    assert pool.pipelines == 1, "pipeline mode was not entered"
    await foundation.aclose()


async def test_sequential_across_data_sources_is_refused(settings):
    app, foundation = await build_app(settings, names=("main", "analytics"))
    body = {
        "mode": "sequential",
        "items": [
            {"id": "a", "datasource": "main", "op": "view", "payload": {"name": "v"}},
            {"id": "b", "datasource": "analytics", "op": "view", "payload": {"name": "v"}},
        ],
    }
    out = (await post_batch(app, body)).json()
    assert out["error_count"] == 2
    assert "single data source" in out["results"][0]["error"]["message"]
    await foundation.aclose()


# --------------------------------------------------------------------------- #
# transactional mode
# --------------------------------------------------------------------------- #
async def test_transactional_commits_all_on_success(settings):
    driver = FakeDriver()
    app, foundation = await build_app(settings, driver=driver)
    response = await post_batch(app, {"mode": "transactional", "items": items(3)})

    assert response.json()["ok_count"] == 3
    pool = next(iter(driver.pools.values()))
    assert (pool.begins, pool.commits, pool.rollbacks) == (1, 1, 0)
    await foundation.aclose()


async def test_transactional_rolls_back_all_on_failure(settings):
    driver = FakeDriver()
    app, foundation = await build_app(settings, driver=driver)
    body = {
        "mode": "transactional",
        "items": [
            {"id": "ok", "datasource": "main", "op": "view", "payload": {"name": "v_ok"}},
            {"id": "bad", "datasource": "main", "op": "view", "payload": {"name": "x; DROP"}},
        ],
    }
    out = (await post_batch(app, body)).json()

    assert out["ok_count"] == 0, "no item may report ok when nothing committed"
    assert out["error_count"] == 2
    pool = next(iter(driver.pools.values()))
    assert pool.commits == 0
    assert pool.rollbacks == 1
    await foundation.aclose()


async def test_transactional_retries_whole_body_on_transient_error(settings):
    """A serialization failure invalidates the transaction, so the WHOLE body
    re-runs — statement-level retry would be insufficient (ADR-010)."""
    attempts = {"n": 0}

    def responder(spec):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise TransientError("40001 serialization failure")
        return ResultSet(rows=[{"id": 1}], rowcount=1)

    driver = FakeDriver(responder)
    app, foundation = await build_app(settings, driver=driver)
    out = (await post_batch(app, {"mode": "transactional", "items": items(1)})).json()

    assert out["ok_count"] == 1
    assert attempts["n"] == 2, "body did not re-run after the transient failure"
    pool = next(iter(driver.pools.values()))
    assert pool.rollbacks == 1 and pool.commits == 1
    await foundation.aclose()


async def test_transactional_across_data_sources_is_refused(settings):
    app, foundation = await build_app(settings, names=("main", "analytics"))
    body = {
        "mode": "transactional",
        "items": [
            {"id": "a", "datasource": "main", "op": "view", "payload": {"name": "v"}},
            {"id": "b", "datasource": "analytics", "op": "view", "payload": {"name": "v"}},
        ],
    }
    out = (await post_batch(app, body)).json()
    assert out["error_count"] == 2
    assert "atomicity cannot span connections" in out["results"][0]["error"]["message"]
    await foundation.aclose()


# --------------------------------------------------------------------------- #
# admission control
# --------------------------------------------------------------------------- #
async def test_admission_rejects_fast_when_saturated():
    """Over-capacity must be a cheap 503, not a slow pool timeout (doc 06 §6.6)."""
    # The bulkhead scenario: the batch may run 4 items at once globally, but a
    # single data source admits only 1. The surplus must be shed quickly rather
    # than queueing into a pool timeout.
    #
    # NB: batch.max_concurrency <= concurrency.max_inflight_total is enforced at
    # boot (doc 05 §5.5), so the pressure has to come from the per-source cap.
    settings = ServiceSettings(
        batch={"max_concurrency": 4},
        concurrency={
            "max_inflight_total": 4,
            "max_inflight_per_datasource": 1,
            "admission_timeout_ms": 50,
        },
    )
    app, foundation = await build_app(settings, driver=SlowDriver(delay=0.3))

    started = time.perf_counter()
    out = (await post_batch(app, {"mode": "parallel", "items": items(4)})).json()
    elapsed = time.perf_counter() - started

    rejected = [r for r in out["results"] if r["status"] == "error"]
    assert rejected, "expected admission rejections under a 1-slot cap"
    assert any("capacity" in r["error"]["message"] for r in rejected)
    # Rejection is bounded by admission_timeout, not by the 0.3 s work.
    assert elapsed < 2.0, f"rejection took {elapsed:.2f}s — not fast-failing"
    await foundation.aclose()


async def test_health_gathers_in_parallel():
    """5 sources x 50 ms => ~50 ms, not ~250 ms (doc 06 §6.4, P2.1)."""
    names = tuple(f"ds{i}" for i in range(5))
    app_settings = load_settings(
        {"datasources": [{"name": n, "dsn": f"postgresql://x/{n}"} for n in names]}
    )
    driver = SlowDriver(delay=0.05)
    foundation = await DataFoundation.from_settings(app_settings, driver=driver)

    started = time.perf_counter()
    health = await foundation.health()
    elapsed = time.perf_counter() - started

    assert len(health) == 5
    assert elapsed < 0.15, f"health took {elapsed:.3f}s; serial would be ~0.25s"
    await foundation.aclose()


@pytest.fixture
def settings() -> ServiceSettings:
    return ServiceSettings()


@pytest.fixture
def settings_small() -> ServiceSettings:
    return ServiceSettings(batch={"max_items": 3})


async def test_batch_items_omit_irrelevant_null_fields(settings):
    """An `ok` item carries no `error`; a failed item carries no `rows`.

    Emitting `"error": null` on every success is noise a client reads past on
    every single response — the same reasoning as `columns` in ADR-007.
    """
    app, foundation = await build_app(settings)
    body = {
        "items": [
            {"id": "ok", "datasource": "main", "op": "view", "payload": {"name": "v_ok"}},
            {"id": "bad", "datasource": "main", "op": "view", "payload": {"name": "x; DROP"}},
        ]
    }
    out = (await post_batch(app, body)).json()
    by_id = {r["id"]: r for r in out["results"]}

    assert "error" not in by_id["ok"]
    assert {"rows", "row_count", "elapsed_ms"} <= set(by_id["ok"])
    assert "rows" not in by_id["bad"]
    assert by_id["bad"]["error"]["type"] == "QueryError"
    await foundation.aclose()

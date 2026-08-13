"""Phase 2 — circuit breaker, resilient pool, metrics emission."""
from __future__ import annotations

import pytest

from pgfoundation import Command, DataFoundation, Query, ResultSet
from pgfoundation._internal.clock import ManualClock
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver
from pgfoundation._internal.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
)
from pgfoundation._internal.resilience.pool import ResilientPool
from pgfoundation.core.errors import ConnectionError as PgConnectionError


# --- Circuit breaker state machine ---

def test_breaker_opens_after_threshold():
    clock = ManualClock()
    cb = CircuitBreaker(failure_threshold=3, reset_timeout_seconds=30, clock=clock)
    assert cb.state is CircuitState.CLOSED
    for _ in range(2):
        cb.on_failure()
    assert cb.allow() is True  # still closed at 2 failures
    cb.on_failure()  # 3rd → trip
    assert cb.state is CircuitState.OPEN
    assert cb.allow() is False


def test_breaker_half_opens_after_reset_then_closes_on_success():
    clock = ManualClock()
    cb = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=30, clock=clock)
    cb.on_failure()
    assert cb.state is CircuitState.OPEN
    clock._t += 30  # advance past reset window
    assert cb.state is CircuitState.HALF_OPEN
    assert cb.allow() is True
    cb.on_success()
    assert cb.state is CircuitState.CLOSED


def test_breaker_reopens_on_half_open_failure():
    clock = ManualClock()
    cb = CircuitBreaker(failure_threshold=1, reset_timeout_seconds=10, clock=clock)
    cb.on_failure()
    clock._t += 10
    assert cb.state is CircuitState.HALF_OPEN
    cb.on_failure()  # probe fails → immediately re-open
    assert cb.state is CircuitState.OPEN


# --- ResilientPool integration ---

async def test_resilient_pool_fast_fails_when_open():
    clock = ManualClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout_seconds=5, clock=clock)

    class _DownPool:
        async def open(self): ...
        async def close(self): ...
        async def acquire(self):
            raise PgConnectionError("db down")
        async def release(self, c): ...
        async def health(self):
            from pgfoundation.core.models import HealthStatus
            return HealthStatus.DOWN

    pool = ResilientPool(_DownPool(), breaker)
    for _ in range(2):
        with pytest.raises(PgConnectionError, match="db down"):
            await pool.acquire()
    # breaker now open → fast-fail without touching the inner pool
    with pytest.raises(PgConnectionError, match="circuit open"):
        await pool.acquire()
    # after reset window it half-opens and probes the inner pool again
    clock._t += 5
    with pytest.raises(PgConnectionError, match="db down"):
        await pool.acquire()


# --- Metrics emission through MetricsPort ---

class RecordingMetrics:
    def __init__(self):
        self.counters: list[tuple] = []
        self.observations: list[tuple] = []

    def incr(self, name, value=1, **labels):
        self.counters.append((name, labels))

    def observe(self, name, value, **labels):
        self.observations.append((name, value, labels))

    def gauge(self, name, value, **labels):
        pass


async def _foundation(responder=None, metrics=None):
    settings = load_settings({"datasources": [{"name": "db", "dsn": "postgresql://x/db"}]})
    driver = FakeDriver(responder)
    db = await DataFoundation.from_settings(settings, driver=driver, metrics=metrics)
    return db


async def test_query_emits_duration_metric():
    def responder(spec):
        return ResultSet(rows=[{"x": 1}], rowcount=1, elapsed_ms=4.2)

    metrics = RecordingMetrics()
    db = await _foundation(responder, metrics)
    await db.query("db", Query("SELECT x"))
    names = [n for n, *_ in metrics.observations]
    assert "pgf_query_duration_seconds" in names
    (name, value, labels) = metrics.observations[0]
    assert labels == {"datasource": "db"}
    assert value == pytest.approx(0.0042)


async def test_query_error_emits_error_counter():
    from pgfoundation.core.errors import QueryError

    def responder(spec):
        raise QueryError("42601 syntax error")

    metrics = RecordingMetrics()
    db = await _foundation(responder, metrics)
    with pytest.raises(QueryError):
        await db.query("db", Query("SELECT bad"))
    assert ("pgf_query_errors_total", {"datasource": "db", "type": "QueryError"}) in metrics.counters

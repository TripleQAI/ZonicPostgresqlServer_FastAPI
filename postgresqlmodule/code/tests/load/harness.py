"""P5.7/P5.8 — the measurement harness (doc 07 §7.9).

Deliberately **not** pytest-benchmark: it times synchronous callables, so every
async measurement would pay for a fresh event loop per iteration and the loop
setup would swamp the thing being measured. This runs N iterations inside one
loop instead.

Two questions it answers, and they are different:

1. **What does the shell cost per request?** Measured against a zero-latency
   driver so framework cost is the only variable. An absolute budget in
   milliseconds — a regression detector.
2. **Does that cost matter?** Measured against a driver with realistic database
   latency, where the honest question is what *fraction* of a request the shell
   adds. This is the one doc 07 §7.9 states a budget for.

The second is the number to quote; the first is the number that catches a
regression before anyone notices the second.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pgfoundation import DataFoundation, ResultSet
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver


@dataclass(frozen=True)
class Timing:
    """Latency distribution in milliseconds."""

    label: str
    n: int
    p50: float
    p95: float
    p99: float
    mean: float

    def __str__(self) -> str:
        return (
            f"{self.label:<34} n={self.n:<5} "
            f"p50={self.p50:7.3f}ms  p95={self.p95:7.3f}ms  "
            f"p99={self.p99:7.3f}ms  mean={self.mean:7.3f}ms"
        )


def summarize(label: str, samples: list[float]) -> Timing:
    ordered = sorted(samples)
    n = len(ordered)

    def pct(p: float) -> float:
        # Nearest-rank: no interpolation, so a single slow sample is visible
        # rather than averaged away.
        return ordered[min(int(p * n), n - 1)] * 1000.0

    return Timing(
        label=label,
        n=n,
        p50=pct(0.50),
        p95=pct(0.95),
        p99=pct(0.99),
        mean=statistics.fmean(ordered) * 1000.0,
    )


async def measure(
    label: str, call: Callable[[], Awaitable[object]], *, n: int = 200, warmup: int = 20
) -> Timing:
    """Time ``n`` sequential calls after discarding ``warmup``.

    Warm-up matters here: the first calls pay for FastAPI's dependency-graph
    resolution and Pydantic's validator compilation, which are one-off costs a
    long-running service pays once at import, not per request.
    """
    for _ in range(warmup):
        await call()

    samples: list[float] = []
    for _ in range(n):
        started = time.perf_counter()
        await call()
        samples.append(time.perf_counter() - started)
    return summarize(label, samples)


def make_driver(latency_s: float = 0.0) -> FakeDriver:
    """A fake driver with an optional simulated round-trip.

    ``latency_s=0`` isolates framework cost. A realistic value (a few
    milliseconds for a same-region database, more across a WAN — doc 12 §12.6)
    shows what share of a real request the shell actually represents.
    """
    rows = [{"id": i, "customer": f"c{i}", "amount": "42.00"} for i in range(10)]

    def responder(spec) -> ResultSet:
        return ResultSet(rows=list(rows), rowcount=len(rows), elapsed_ms=latency_s * 1000)

    driver = FakeDriver(responder)
    if latency_s <= 0:
        return driver

    original_build = driver.build_pool

    def build_pool(dsn, policy):
        pool = original_build(dsn, policy)
        acquire = pool.acquire

        async def slow_acquire():
            conn = await acquire()
            inner = conn.execute

            async def execute(spec, *, row_format="dict"):
                await asyncio.sleep(latency_s)
                return await inner(spec, row_format=row_format)

            conn.execute = execute  # type: ignore[method-assign]
            return conn

        pool.acquire = slow_acquire  # type: ignore[method-assign]
        return pool

    driver.build_pool = build_pool  # type: ignore[method-assign]
    return driver


async def build_foundation(latency_s: float = 0.0, names: tuple[str, ...] = ("main",)):
    settings = load_settings(
        {"datasources": [{"name": n, "dsn": f"postgresql://x/{n}"} for n in names]}
    )
    return await DataFoundation.from_settings(settings, driver=make_driver(latency_s))


__all__ = ["Timing", "build_foundation", "make_driver", "measure", "summarize"]

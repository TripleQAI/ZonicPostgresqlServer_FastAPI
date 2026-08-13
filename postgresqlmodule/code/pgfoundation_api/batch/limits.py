"""L4 — admission control and deadlines (doc 06 §6.6).

Three nested bounds, each config-driven:

1. **Global in-flight cap** — total concurrent DB operations in this process.
2. **Per-datasource cap** — the bulkhead; one hot database cannot take every slot.
3. **Pool ``max_size`` + acquire timeout** — the existing innermost bound.

The point of the outer two is *load shedding*. Without them, "more concurrency"
just relocates the queue from the connection pool into the event loop, and every
request pays a full pool-acquire timeout instead of a fast rejection. Rejection
costs microseconds; a pool timeout costs the whole acquire budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pgfoundation import PoolTimeoutError

from ..settings import ConcurrencySettings


class AdmissionRejected(PoolTimeoutError):
    """No slot became free within the admission budget.

    Subclasses ``PoolTimeoutError`` so it maps to ``503`` through the existing
    error table with no new mapping — semantically it *is* a saturation signal,
    just detected earlier and far more cheaply.
    """


class AdmissionLimiter:
    """Bounded admission: global first, then per data source."""

    def __init__(self, settings: ConcurrencySettings) -> None:
        self._settings = settings
        self._global = asyncio.Semaphore(settings.max_inflight_total)
        self._per_source: dict[str, asyncio.Semaphore] = {}
        self._timeout = settings.admission_timeout_ms / 1000.0
        # Observability + tests need to see how close to the cap we run.
        self.in_flight = 0
        self.peak_in_flight = 0

    def _source_semaphore(self, datasource: str) -> asyncio.Semaphore:
        semaphore = self._per_source.get(datasource)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._settings.max_inflight_per_datasource)
            self._per_source[datasource] = semaphore
        return semaphore

    @asynccontextmanager
    async def slot(self, datasource: str) -> AsyncIterator[None]:
        """Acquire a global slot then a per-source slot, both time-bounded."""
        # OBSERVE (METRIC): pgf_admission_wait_seconds{scope="global"}
        # OBSERVE (METRIC): pgf_admission_rejected_total{scope, datasource}
        # OBSERVE (LOG):    admission.rejected {scope, waited_ms}
        await self._acquire(self._global, scope="global", datasource=datasource)
        try:
            # OBSERVE (METRIC): pgf_admission_wait_seconds{scope="datasource"}
            await self._acquire(
                self._source_semaphore(datasource), scope="datasource", datasource=datasource
            )
        except BaseException:
            self._global.release()
            raise

        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        # OBSERVE (METRIC): pgf_admission_slots_inuse{scope, datasource} gauge
        try:
            yield
        finally:
            self.in_flight -= 1
            self._source_semaphore(datasource).release()
            self._global.release()

    async def _acquire(self, semaphore: asyncio.Semaphore, *, scope: str, datasource: str) -> None:
        try:
            async with asyncio.timeout(self._timeout):
                await semaphore.acquire()
        except TimeoutError:
            raise AdmissionRejected(
                f"service at capacity ({scope} limit) — no slot for {datasource!r} "
                f"within {self._settings.admission_timeout_ms}ms"
            ) from None


__all__ = ["AdmissionLimiter", "AdmissionRejected"]

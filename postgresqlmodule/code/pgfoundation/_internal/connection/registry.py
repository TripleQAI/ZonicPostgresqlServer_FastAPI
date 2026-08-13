"""L1 — the Connection Registry: owns every named data-source pool (doc 06).

The heart of "multiple PostgreSQL connections" (Requirement 1). Built once at
the composition root; the request path only does O(1) name lookups.
"""
from __future__ import annotations

from ...core.errors import ConfigError
from ...core.models import DataSourceName, HealthStatus
from ...core.ports import ClockPort, DriverPort, MetricsPort, PoolPort
from ..clock import SystemClock
from ..config.settings import DataSourceSettings, ResilienceSettings
from ..observability.metered import MeteredPool
from ..observability.noop import NoopMetrics
from ..resilience.circuit_breaker import CircuitBreaker
from ..resilience.pool import ResilientPool


class PoolFactory:
    """Abstract Factory — builds a PoolPort, then wraps it with the configured

    resilience (circuit breaker) and observability (metrics) decorators. Each
    data source gets its **own** breaker → bulkhead isolation (doc 06 §6.3).
    """

    def __init__(
        self,
        driver: DriverPort,
        *,
        resilience: ResilienceSettings | None = None,
        metrics: MetricsPort | None = None,
        clock: ClockPort | None = None,
    ) -> None:
        self._driver = driver
        self._resilience = resilience or ResilienceSettings()
        self._metrics = metrics or NoopMetrics()
        self._clock = clock or SystemClock()

    def build(self, settings: DataSourceSettings) -> PoolPort:
        dsn = settings.dsn.get_secret_value()
        pool: PoolPort = self._driver.build_pool(dsn, settings.to_pool_policy())

        cb = self._resilience.circuit_breaker
        if cb.enabled:
            breaker = CircuitBreaker(
                failure_threshold=cb.failure_threshold,
                reset_timeout_seconds=cb.reset_timeout_seconds,
                clock=self._clock,
            )
            pool = ResilientPool(pool, breaker)

        return MeteredPool(pool, self._metrics, settings.name)


class ConnectionRegistry:
    """Registry pattern — ``DataSourceName -> PoolPort`` (Bulkhead per source)."""

    def __init__(self, factory: PoolFactory) -> None:
        self._factory = factory
        self._pools: dict[DataSourceName, PoolPort] = {}

    def register(self, settings: DataSourceSettings) -> None:
        if settings.name in self._pools:
            raise ConfigError(f"data source already registered: {settings.name!r}")
        self._pools[settings.name] = self._factory.build(settings)

    def get(self, name: DataSourceName) -> PoolPort:
        try:
            return self._pools[name]
        except KeyError:
            raise ConfigError(
                f"unknown data source {name!r}; known: {sorted(self._pools)}"
            ) from None

    def names(self) -> list[DataSourceName]:
        return list(self._pools)

    async def start(self) -> None:
        """Open every pool concurrently.

        Serially, startup cost is the *sum* of connect times; concurrently it is
        the max (doc 06 §6.4).
        """
        await asyncio.gather(*(pool.open() for pool in self._pools.values()))

    async def aclose(self) -> None:
        """Close every pool concurrently.

        ``return_exceptions=True`` so one pool failing to close cannot leave the
        others open — shutdown must drain everything it can.
        """
        await asyncio.gather(
            *(pool.close() for pool in self._pools.values()), return_exceptions=True
        )

    async def health(self) -> dict[DataSourceName, HealthStatus]:
        """Probe every data source concurrently.

        Serially this took the *sum* of the per-source probe times: with 5 sources
        and a 3 s acquire timeout, a health check against a degraded cluster could
        take 15 s and trip a readiness probe that would otherwise have passed.
        Concurrently it takes the slowest single probe (doc 06 §6.4).

        A probe that raises is reported as DOWN rather than failing the whole
        check — one unreachable source must not hide the state of the others.
        """
        names = list(self._pools)
        results = await asyncio.gather(
            *(self._pools[name].health() for name in names), return_exceptions=True
        )
        return {
            name: (HealthStatus.DOWN if isinstance(result, BaseException) else result)
            for name, result in zip(names, results, strict=True)
        }


__all__ = ["ConnectionRegistry", "PoolFactory"]

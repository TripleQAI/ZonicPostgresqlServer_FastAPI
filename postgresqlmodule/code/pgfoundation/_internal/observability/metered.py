"""Metering decorators — emit through MetricsPort (doc 10 §10.1 / ADR-014).

The foundation only *emits* the signal vocabulary; an external log project binds
the pipeline. With ``NoopMetrics`` this is a couple of no-op attribute lookups.
"""
from __future__ import annotations

from typing import Any

from ...core.errors import PgFoundationError
from ...core.models import HealthStatus, QuerySpec, ResultSet
from ...core.ports import ConnectionPort, MetricsPort, PoolPort


class MeteredConnection:
    """Times ``execute``/``execute_many`` and counts errors; delegates the rest."""

    def __init__(self, inner: ConnectionPort, metrics: MetricsPort, ds: str) -> None:
        self._inner = inner
        self._metrics = metrics
        self._ds = ds

    async def execute(self, spec: QuerySpec) -> ResultSet:
        try:
            rs = await self._inner.execute(spec)
        except PgFoundationError as exc:
            self._metrics.incr(
                "pgf_query_errors_total", datasource=self._ds, type=type(exc).__name__
            )
            raise
        self._metrics.observe(
            "pgf_query_duration_seconds", rs.elapsed_ms / 1000.0, datasource=self._ds
        )
        return rs

    async def execute_many(self, spec: QuerySpec, params_seq) -> int:
        try:
            return await self._inner.execute_many(spec, params_seq)
        except PgFoundationError as exc:
            self._metrics.incr(
                "pgf_query_errors_total", datasource=self._ds, type=type(exc).__name__
            )
            raise

    def __getattr__(self, name: str) -> Any:
        # stream / copy_stream / listen / begin delegate unchanged.
        return getattr(self._inner, name)


class MeteredPool:
    """Wraps a pool so acquired connections are metered; tracks pool gauges."""

    def __init__(self, inner: PoolPort, metrics: MetricsPort, ds: str) -> None:
        self._inner = inner
        self._metrics = metrics
        self._ds = ds

    async def open(self) -> None:
        await self._inner.open()

    async def close(self) -> None:
        await self._inner.close()

    async def acquire(self) -> MeteredConnection:
        try:
            conn = await self._inner.acquire()
        except PgFoundationError as exc:
            self._metrics.incr(
                "pgf_pool_timeouts_total"
                if type(exc).__name__ == "PoolTimeoutError"
                else "pgf_pool_errors_total",
                datasource=self._ds,
            )
            raise
        return MeteredConnection(conn, self._metrics, self._ds)

    async def release(self, conn: MeteredConnection) -> None:
        inner = conn._inner if isinstance(conn, MeteredConnection) else conn
        await self._inner.release(inner)

    async def health(self) -> HealthStatus:
        return await self._inner.health()


__all__ = ["MeteredPool", "MeteredConnection"]

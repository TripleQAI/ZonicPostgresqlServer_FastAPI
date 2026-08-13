"""ResilientPool — a Decorator that adds circuit breaking to a PoolPort (doc 10)."""
from __future__ import annotations

from ...core.errors import ConnectionError as PgConnectionError
from ...core.errors import PoolTimeoutError
from ...core.models import HealthStatus
from ...core.ports import ConnectionPort, PoolPort
from .circuit_breaker import CircuitBreaker


class ResilientPool:
    """Wraps a pool: opens the breaker on repeated connection failures, then

    fast-fails ``acquire`` until the reset window (doc 10 §10.3).
    """

    def __init__(self, inner: PoolPort, breaker: CircuitBreaker) -> None:
        self._inner = inner
        self._breaker = breaker

    async def open(self) -> None:
        await self._inner.open()

    async def close(self) -> None:
        await self._inner.close()

    async def acquire(self) -> ConnectionPort:
        if not self._breaker.allow():
            raise PgConnectionError("circuit open — data source unavailable")
        try:
            conn = await self._inner.acquire()
        except (PgConnectionError, PoolTimeoutError):
            self._breaker.on_failure()
            raise
        self._breaker.on_success()
        return conn

    async def release(self, conn: ConnectionPort) -> None:
        await self._inner.release(conn)

    async def health(self) -> HealthStatus:
        return await self._inner.health()


__all__ = ["ResilientPool"]

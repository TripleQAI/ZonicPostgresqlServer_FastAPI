"""psycopg 3 adapter — the only place psycopg is imported (ADR-001, doc 12 §12.3).

Implements the Core ``DriverPort`` / ``PoolPort`` / ``ConnectionPort`` seams over
``psycopg_pool.AsyncConnectionPool``.
"""
from __future__ import annotations

import time
from typing import Any, AsyncIterator

from typing import Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from ...core.errors import PoolTimeoutError
from ...core.models import (
    HealthStatus,
    IsolationLevel,
    NotifyEvent,
    PoolPolicy,
    QuerySpec,
    Row,
    ResultSet,
)
from .errors_map import map_error


async def _as_aiter(rows):
    if hasattr(rows, "__aiter__"):
        async for r in rows:
            yield r
    else:
        for r in rows:
            yield r

_ISO = {
    IsolationLevel.READ_COMMITTED: psycopg.IsolationLevel.READ_COMMITTED,
    IsolationLevel.REPEATABLE_READ: psycopg.IsolationLevel.REPEATABLE_READ,
    IsolationLevel.SERIALIZABLE: psycopg.IsolationLevel.SERIALIZABLE,
}


class PsycopgConnection:
    """Wraps one psycopg async connection behind ``ConnectionPort``."""

    def __init__(self, conn: "psycopg.AsyncConnection[Any]") -> None:
        self._conn = conn

    @property
    def raw(self) -> "psycopg.AsyncConnection[Any]":
        return self._conn

    async def execute(self, spec: QuerySpec) -> ResultSet:
        start = time.perf_counter()
        try:
            async with self._conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(spec.sql, spec.params)
                rows: list[Row] = await cur.fetchall() if cur.description else []
                rowcount = cur.rowcount
        except psycopg.Error as exc:
            raise map_error(exc) from exc
        return ResultSet(
            rows=rows,
            rowcount=rowcount,
            elapsed_ms=(time.perf_counter() - start) * 1000.0,
        )

    async def execute_many(self, spec: QuerySpec, params_seq: Sequence) -> int:
        """Batched parameterized writes (doc 07 §7.8)."""
        try:
            async with self._conn.cursor() as cur:
                await cur.executemany(spec.sql, list(params_seq))
                return cur.rowcount
        except psycopg.Error as exc:
            raise map_error(exc) from exc

    async def stream(
        self, spec: QuerySpec, *, itersize: int = 1000
    ) -> AsyncIterator[Row]:
        """Server-side cursor — constant memory (doc 16 §16.3).

        A named (server-side) cursor requires a transaction block; under
        autocommit we open a short read transaction for the stream's lifetime.
        """
        try:
            async with self._conn.transaction():
                async with self._conn.cursor(
                    name=f"pgf_stream_{id(spec)}", row_factory=dict_row
                ) as cur:
                    cur.itersize = itersize
                    await cur.execute(spec.sql, spec.params)
                    async for row in cur:
                        yield row
        except psycopg.Error as exc:
            raise map_error(exc) from exc

    async def copy_stream(self, table: str, rows) -> int:
        """Streaming bulk ingest via COPY — flat memory (doc 16 §16.6).

        ``table`` must already be a validated identifier (checked at the handle).
        """
        n = 0
        try:
            async with self._conn.cursor() as cur:
                async with cur.copy(f"COPY {table} FROM STDIN") as cp:
                    async for row in _as_aiter(rows):
                        await cp.write_row(row)
                        n += 1
        except psycopg.Error as exc:
            raise map_error(exc) from exc
        return n

    async def listen(self, channel: str) -> AsyncIterator[NotifyEvent]:
        """LISTEN/NOTIFY change stream (doc 16 §16.6). ``channel`` pre-validated."""
        try:
            await self._conn.execute(f"LISTEN {channel}")
            async for note in self._conn.notifies():
                yield NotifyEvent(
                    channel=note.channel, payload=note.payload, pid=note.pid
                )
        except psycopg.Error as exc:
            raise map_error(exc) from exc

    def begin(self, isolation: IsolationLevel | None = None):
        """Transaction context; nested calls create savepoints (doc 07 §7.5).

        psycopg sets isolation on the *connection*, not per-transaction, so we set
        it before the block and restore it after (pool connections are reused).
        """
        return _PsycopgTransaction(self._conn, isolation)


class _PsycopgTransaction:
    def __init__(self, conn, isolation: IsolationLevel | None) -> None:
        self._conn = conn
        self._isolation = isolation
        self._txn = None
        self._prev = None

    async def __aenter__(self):
        if self._isolation is not None:
            self._prev = self._conn.isolation_level
            await self._conn.set_isolation_level(_ISO[self._isolation])
        self._txn = self._conn.transaction()
        await self._txn.__aenter__()
        return self._txn

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._txn.__aexit__(exc_type, exc, tb)
        finally:
            if self._isolation is not None:
                await self._conn.set_isolation_level(self._prev)


class PsycopgPool:
    """Wraps one ``AsyncConnectionPool`` behind ``PoolPort``."""

    def __init__(self, dsn: str, policy: PoolPolicy) -> None:
        self._policy = policy
        # Bound each connect attempt so an unreachable DB fails FAST instead of
        # hanging — otherwise the pool's reconnect worker stalls shutdown and can
        # leave the process (and its port) held open. Respect an explicit
        # connect_timeout already present in the DSN.
        connect_kwargs = {}
        if "connect_timeout" not in dsn:
            connect_kwargs["connect_timeout"] = max(2, int(policy.acquire_timeout_seconds))
        self._pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=policy.min_size,
            max_size=policy.max_size,
            max_idle=policy.max_idle_seconds,
            max_lifetime=policy.max_lifetime_seconds,
            timeout=policy.acquire_timeout_seconds,
            kwargs=connect_kwargs,
            open=False,
            configure=self._configure,
        )

    async def _configure(self, conn: "psycopg.AsyncConnection[Any]") -> None:
        # On async connections autocommit must be set via the awaitable setter.
        await conn.set_autocommit(True)  # explicit transactions managed by the UoW
        if self._policy.read_only:
            await conn.execute("SET default_transaction_read_only = on")
        if self._policy.statement_timeout_ms is not None:
            await conn.execute(
                "SET statement_timeout = %s", (self._policy.statement_timeout_ms,)
            )

    async def open(self) -> None:
        # Open the pool but do NOT hard-fail startup if the DB is momentarily
        # down — connections fill lazily and health() reports status, so the
        # service stays up and flips readiness (graceful degradation, doc 06 §6.7).
        await self._pool.open()
        try:
            await self._pool.wait(timeout=self._policy.acquire_timeout_seconds)
        except Exception:
            pass  # warmup best-effort; first query still waits up to acquire timeout

    async def close(self) -> None:
        await self._pool.close()

    async def acquire(self) -> PsycopgConnection:
        try:
            conn = await self._pool.getconn()
        except psycopg.pool.PoolTimeout as exc:  # type: ignore[attr-defined]
            raise PoolTimeoutError(str(exc)) from exc
        return PsycopgConnection(conn)

    async def release(self, conn: PsycopgConnection) -> None:
        await self._pool.putconn(conn.raw)

    async def health(self) -> HealthStatus:
        try:
            conn = await self.acquire()
            try:
                await conn.execute(QuerySpec("SELECT 1"))
            finally:
                await self.release(conn)
            return HealthStatus.HEALTHY
        except Exception:
            return HealthStatus.DOWN


class PsycopgDriver:
    """``DriverPort`` — the factory that builds psycopg pools."""

    def build_pool(self, dsn: str, policy: PoolPolicy) -> PsycopgPool:
        return PsycopgPool(dsn, policy)


__all__ = ["PsycopgDriver", "PsycopgPool", "PsycopgConnection"]

"""L3 — per-data-source handle: the ergonomic basic-API surface (doc 11 §11.4a)."""
from __future__ import annotations

from typing import Any, AsyncIterator, Sequence

from ...core.models import (
    Command,
    DataSourceName,
    FunctionQuery,
    HealthStatus,
    IntervalQuery,
    IsolationLevel,
    NotifyEvent,
    Page,
    ProcedureCall,
    Query,
    QuerySpec,
    ResultSet,
    RetryPolicy,
    Row,
    ViewQuery,
    WatermarkCursor,
)
from ...core.ports import ClockPort, PoolPort
from ..access.interval import compile_interval
from ..access.invocation import compile_function, compile_procedure, compile_view
from ..access.sql import safe_identifier
from ..access.transactions import TxnBody, run_transaction
from ..access.uow import UnitOfWork
from ..access.watermark import fetch_since as _fetch_since
from ..clock import SystemClock


class DataSourceHandle:
    """Bind-once, reuse wrapper over one pool. Read / Write / Transaction / Stream."""

    def __init__(
        self,
        name: DataSourceName,
        pool: PoolPort,
        clock: ClockPort | None = None,
    ) -> None:
        self.name = name
        self._pool = pool
        self._clock = clock or SystemClock()

    # --- Read ---
    async def query(self, spec: Query | QuerySpec) -> ResultSet:
        conn = await self._pool.acquire()
        try:
            return await conn.execute(spec)
        finally:
            await self._pool.release(conn)

    async def one(self, spec: Query | QuerySpec) -> Row | None:
        rs = await self.query(spec)
        return rs.rows[0] if rs.rows else None

    async def scalar(self, spec: Query | QuerySpec) -> Any:
        row = await self.one(spec)
        if not row:
            return None
        return next(iter(row.values()))

    # --- Write ---
    async def execute(self, cmd: Command | QuerySpec) -> int:
        rs = await self.query(cmd)
        return rs.rowcount

    async def execute_many(
        self, cmd: Command | QuerySpec, params_seq: Sequence[Any]
    ) -> int:
        conn = await self._pool.acquire()
        try:
            return await conn.execute_many(cmd, params_seq)
        finally:
            await self._pool.release(conn)

    # --- Transactions (doc 07 §7.5, §7.10) ---
    def transaction(self, *, isolation: IsolationLevel | None = None) -> UnitOfWork:
        return UnitOfWork(self._pool, isolation)

    async def run_transaction(
        self,
        fn: TxnBody,
        *,
        isolation: IsolationLevel | None = None,
        retry: RetryPolicy | None = None,
    ) -> Any:
        return await run_transaction(
            self._pool, fn, isolation=isolation, retry=retry, clock=self._clock
        )

    # --- Streaming (pinned connection for the stream's lifetime) ---
    async def stream(
        self, spec: Query | QuerySpec, *, itersize: int = 1000
    ) -> AsyncIterator[Row]:
        conn = await self._pool.acquire()
        try:
            async for row in conn.stream(spec, itersize=itersize):
                yield row
        finally:
            await self._pool.release(conn)

    # --- Governed object invocation: call DB objects by NAME, not raw SQL ---
    async def view(self, spec: ViewQuery) -> ResultSet:
        """Query a view/table by name with structured filters (compiled to safe SQL)."""
        return await self.query(compile_view(spec))

    def view_stream(
        self, spec: ViewQuery, *, itersize: int = 1000
    ) -> AsyncIterator[Row]:
        """Stream a large view result at constant memory (high-throughput reads)."""
        return self.stream(compile_view(spec), itersize=itersize)

    async def function(self, spec: FunctionQuery) -> ResultSet:
        """Invoke a function by name (set-returning or scalar)."""
        return await self.query(compile_function(spec))

    def function_stream(
        self, spec: FunctionQuery, *, itersize: int = 1000
    ) -> AsyncIterator[Row]:
        return self.stream(compile_function(spec), itersize=itersize)

    async def procedure(self, spec: ProcedureCall) -> ResultSet:
        """Invoke a stored procedure by name via CALL (returns OUT/INOUT rows)."""
        return await self.query(compile_procedure(spec))

    # --- Interval / windowed fetch (doc 16 §16.4) ---
    def interval(
        self, spec: IntervalQuery, *, itersize: int = 1000
    ) -> AsyncIterator[Row]:
        return self.stream(compile_interval(spec), itersize=itersize)

    # --- Resumable incremental fetch (doc 16 §16.5) ---
    async def fetch_since(
        self, spec: Query, cursor: WatermarkCursor, *, limit: int
    ) -> Page:
        return await _fetch_since(self, spec, cursor, limit=limit)

    # --- Streaming ingest / change stream (doc 16 §16.6) ---
    async def copy_stream(self, table: str, rows) -> int:
        tbl = safe_identifier(table)
        conn = await self._pool.acquire()
        try:
            return await conn.copy_stream(tbl, rows)
        finally:
            await self._pool.release(conn)

    async def listen(self, channel: str) -> AsyncIterator[NotifyEvent]:
        ch = safe_identifier(channel)
        conn = await self._pool.acquire()
        try:
            async for event in conn.listen(ch):
                yield event
        finally:
            await self._pool.release(conn)

    # --- Admin ---
    async def health(self) -> HealthStatus:
        return await self._pool.health()


__all__ = ["DataSourceHandle"]

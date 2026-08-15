"""L3 — per-data-source handle: the ergonomic basic-API surface (doc 11 §11.4a)."""
from __future__ import annotations

from typing import Any, AsyncIterator, Sequence

from ...core.errors import QueryError
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
from ..access.sql import quote_identifier
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
        itersize: int = 1000,
        row_format: str = "dict",
    ) -> None:
        self.name = name
        self._pool = pool
        self._clock = clock or SystemClock()
        self._itersize = itersize
        # P5.1 — bound on the handle rather than threaded through all nine
        # operations. `foundation.datasource(name, row_format=...)` returns a
        # handle configured for that shape (doc 07 §7.5).
        self._row_format = row_format

    # --- Read ---
    async def query(
        self, spec: Query | QuerySpec, *, timeout_ms: int | None = None
    ) -> ResultSet:
        conn = await self._pool.acquire()
        try:
            if timeout_ms is not None:
                # P4.4 — the half that actually stops work. Cancelling the
                # coroutine abandons the query; PostgreSQL keeps executing it for
                # a socket nobody reads. SET LOCAL scopes the change to this
                # statement's transaction, so a pooled connection is not left
                # carrying a foreign timeout.
                await conn.execute(
                    QuerySpec(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                )
            return await conn.execute(spec, row_format=self._row_format)
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
    async def execute(
        self, cmd: Command | QuerySpec, *, timeout_ms: int | None = None
    ) -> int:
        # P4.6 — a write does not travel the read path. Today both acquire the
        # same pool, but keeping the paths distinct is what lets a future
        # RoutingStrategy send reads to a replica and writes to the primary
        # without re-plumbing every caller (doc 02 §2.6).
        conn = await self._pool.acquire()
        try:
            if timeout_ms is not None:
                await conn.execute(
                    QuerySpec(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                )
            result = await conn.execute(cmd)
            return result.rowcount
        finally:
            await self._pool.release(conn)

    async def execute_many(
        self, cmd: Command | QuerySpec, params_seq: Sequence[Any]
    ) -> int:
        conn = await self._pool.acquire()
        try:
            return await conn.execute_many(cmd, params_seq)
        finally:
            await self._pool.release(conn)

    def compile_spec(self, obj: Any) -> QuerySpec:
        """Turn a governed value object into a parameterized :class:`QuerySpec`.

        ``view()`` / ``function()`` / ``procedure()`` compile internally and then
        execute, which is right for a single call. Callers that need to run
        several governed operations on **one pinned connection** — a sequential
        or transactional batch (doc 06 §6.3) — need the compiled spec without the
        execution, and must not reach into ``_internal`` to get it.

        ``Query`` and ``Command`` are already specs and pass through unchanged.
        """
        if isinstance(obj, QuerySpec):
            return obj
        if isinstance(obj, ViewQuery):
            return compile_view(obj)
        if isinstance(obj, FunctionQuery):
            return compile_function(obj)
        if isinstance(obj, ProcedureCall):
            return compile_procedure(obj)
        if isinstance(obj, IntervalQuery):
            return compile_interval(obj)
        raise QueryError(f"cannot compile {type(obj).__name__} into a QuerySpec")

    async def run_pipeline(
        self, specs: Sequence[Query | QuerySpec]
    ) -> list[ResultSet]:
        """Run N statements on ONE pinned connection, pipelined where supported.

        The ordered, low-round-trip counterpart to fanning out across N
        connections (doc 06 §6.3). Statements execute in order and results are
        returned in order.

        Not a transaction: there is no atomicity here. Use ``run_transaction``
        when all-or-nothing is required.
        """
        conn = await self._pool.acquire()
        try:
            async with conn.pipeline():
                return [await conn.execute(spec) for spec in specs]
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
        self, spec: Query | QuerySpec, *, itersize: int | None = None
    ) -> AsyncIterator[Row]:
        """Stream at constant memory.

        ``itersize`` defaults to the data source's configured value (P5.3) rather
        than a literal, so a long-scan source can fetch in larger batches than an
        OLTP one without every caller passing it.
        """
        size = itersize if itersize is not None else self._itersize
        conn = await self._pool.acquire()
        try:
            async for row in conn.stream(spec, itersize=size):
                yield row
        finally:
            await self._pool.release(conn)

    # --- Governed object invocation: call DB objects by NAME, not raw SQL ---
    async def view(self, spec: ViewQuery) -> ResultSet:
        """Query a view/table by name with structured filters (compiled to safe SQL)."""
        return await self.query(compile_view(spec))

    def view_stream(
        self, spec: ViewQuery, *, itersize: int | None = None
    ) -> AsyncIterator[Row]:
        """Stream a large view result at constant memory (high-throughput reads)."""
        return self.stream(compile_view(spec), itersize=itersize)

    async def function(self, spec: FunctionQuery) -> ResultSet:
        """Invoke a function by name (set-returning or scalar)."""
        return await self.query(compile_function(spec))

    def function_stream(
        self, spec: FunctionQuery, *, itersize: int | None = None
    ) -> AsyncIterator[Row]:
        return self.stream(compile_function(spec), itersize=itersize)

    async def procedure(self, spec: ProcedureCall) -> ResultSet:
        """Invoke a stored procedure by name via CALL (returns OUT/INOUT rows)."""
        return await self.query(compile_procedure(spec))

    # --- Interval / windowed fetch (doc 16 §16.4) ---
    def interval(
        self, spec: IntervalQuery, *, itersize: int | None = None
    ) -> AsyncIterator[Row]:
        return self.stream(compile_interval(spec), itersize=itersize)

    async def interval_page(self, spec: IntervalQuery) -> ResultSet:
        """Materialized interval fetch, for callers that want one response.

        ``interval()`` streams, which is right for a long range. A bucketed
        dashboard query over a bounded window is a normal request/response, and
        forcing it through a stream would make every caller reassemble it.
        """
        return await self.query(compile_interval(spec))

    # --- Resumable incremental fetch (doc 16 §16.5) ---
    async def fetch_since(
        self, spec: Query, cursor: WatermarkCursor, *, limit: int
    ) -> Page:
        return await _fetch_since(self, spec, cursor, limit=limit)

    # --- Streaming ingest / change stream (doc 16 §16.6) ---
    async def copy_stream(self, table: str, rows) -> int:
        tbl = quote_identifier(table)
        conn = await self._pool.acquire()
        try:
            return await conn.copy_stream(tbl, rows)
        finally:
            await self._pool.release(conn)

    async def listen(self, channel: str) -> AsyncIterator[NotifyEvent]:
        ch = quote_identifier(channel)
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

"""psycopg 3 adapter — the only place psycopg is imported (ADR-001, doc 12 §12.3).

Implements the Core ``DriverPort`` / ``PoolPort`` / ``ConnectionPort`` seams over
``psycopg_pool.AsyncConnectionPool``.
"""
from __future__ import annotations

import itertools
import time
from typing import Any, AsyncIterator

from typing import Sequence

import psycopg
from psycopg.rows import dict_row, tuple_row
from psycopg_pool import AsyncConnectionPool

from ...core.errors import PoolTimeoutError
from ...core.ports import ConnectionPort
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

# P3.1 — server-side cursor names must be unique per connection.
# The original used `id(spec)`, which is a memory address and IS REUSED after
# garbage collection: two concurrent streams could pick the same name and
# collide. A process-wide monotonic counter cannot repeat.
_CURSOR_SEQ = itertools.count()


def session_timeout_sql(milliseconds: int) -> str:
    """Build ``SET statement_timeout = <ms>``.

    ``SET`` is a **utility statement**: PostgreSQL parses it directly and does
    not accept bound parameters, so ``SET statement_timeout = %s`` fails with
    `syntax error at or near "$1"` the moment psycopg sends it over the extended
    protocol. The value has to be inlined.

    Inlining is safe here and only here: ``int()`` is a total function to a
    number, and the input is an already-validated config field. This is NOT a
    licence to interpolate elsewhere — every value on the query path stays a
    bound parameter (doc 07 §7.2).

    Kept as a function so the SQL can be asserted without a database; the bug it
    replaces was invisible to every test because no configuration set a
    statement timeout.
    """
    return f"SET statement_timeout = {int(milliseconds)}"

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

    async def execute(
        self, spec: QuerySpec, *, row_format: str = "dict"
    ) -> ResultSet:
        start = time.perf_counter()
        # P5.1 — the row factory IS the mechanism (doc 07 §7.5). Converting dicts
        # back into tuples in Python afterwards would spend exactly the time the
        # tuple format exists to save.
        factory = tuple_row if row_format == "tuple" else dict_row
        columns: list[str] | None = None
        try:
            async with self._conn.cursor(row_factory=factory) as cur:
                await cur.execute(spec.sql, spec.params)
                rows: list[Any] = await cur.fetchall() if cur.description else []
                rowcount = cur.rowcount
                if row_format == "tuple" and cur.description:
                    columns = [d.name for d in cur.description]
        except psycopg.Error as exc:
            raise map_error(exc) from exc
        return ResultSet(
            rows=rows,
            rowcount=rowcount,
            elapsed_ms=(time.perf_counter() - start) * 1000.0,
            columns=columns,
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
                    name=f"pgf_stream_{next(_CURSOR_SEQ)}", row_factory=dict_row
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

    def pipeline(self):
        """psycopg pipeline mode — many statements, far fewer round-trips.

        The win is largest when RTT dominates query time, which is the normal
        case against a managed cloud database (doc 12 §12.6). Note that fetching
        a result forces a sync point, so the saving applies to the statements
        queued between syncs rather than collapsing every batch to exactly one
        round-trip.
        """
        return self._conn.pipeline()

    def begin(self, isolation: IsolationLevel | None = None):
        """Transaction context; nested calls create savepoints (doc 07 §7.5).

        psycopg sets isolation on the *connection*, not per-transaction, so it is
        set before the block and restored after.

        **P4.7 — the invariant this depends on.** That is only safe because a
        ``UnitOfWork`` PINS one connection for the whole transaction: no other
        coroutine can observe the connection while its isolation level is
        changed. If a future change ever let a transaction span connections, or
        returned the connection to the pool mid-transaction, this would silently
        leak one caller's isolation level into another's work.

        The invariant is asserted in ``UnitOfWork.__aenter__`` rather than left
        as a comment, so a refactor that breaks it fails loudly.
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
        self._dsn = dsn
        self._policy = policy
        #: Last warm-up failure, or None. Surfaced by health/readiness (P4.3).
        self.last_warmup_error: str | None = None
        #: Consecutive health() failures since the last success or reset — see
        #: health()'s comment on why this triggers a pool rebuild.
        self._consecutive_health_failures = 0
        self._pool = self._build_pool()

    def _build_pool(self) -> AsyncConnectionPool:
        # Bound each connect attempt so an unreachable DB fails FAST instead of
        # hanging — otherwise the pool's reconnect worker stalls shutdown and can
        # leave the process (and its port) held open. Respect an explicit
        # connect_timeout already present in the DSN.
        connect_kwargs = {}
        if "connect_timeout" not in self._dsn:
            connect_kwargs["connect_timeout"] = max(2, int(self._policy.acquire_timeout_seconds))
        return AsyncConnectionPool(
            conninfo=self._dsn,
            min_size=self._policy.min_size,
            max_size=self._policy.max_size,
            max_idle=self._policy.max_idle_seconds,
            max_lifetime=self._policy.max_lifetime_seconds,
            timeout=self._policy.acquire_timeout_seconds,
            kwargs=connect_kwargs,
            open=False,
            configure=self._configure,
        )

    async def _configure(self, conn: "psycopg.AsyncConnection[Any]") -> None:
        # On async connections autocommit must be set via the awaitable setter.
        await conn.set_autocommit(True)  # explicit transactions managed by the UoW
        # P5.2 — prepared-statement tuning from config, not driver defaults.
        # The invocation compilers emit deterministic SQL per call shape, so a
        # low threshold pays off: PostgreSQL stops re-planning the same query.
        conn.prepare_threshold = self._policy.prepare_threshold
        conn.prepared_max = self._policy.prepare_max_cached
        if self._policy.read_only:
            await conn.execute("SET default_transaction_read_only = on")
        if self._policy.statement_timeout_ms is not None:
            await conn.execute(session_timeout_sql(self._policy.statement_timeout_ms))

    async def open(self) -> None:
        # Open the pool but do NOT hard-fail startup if the DB is momentarily
        # down — connections fill lazily and health() reports status, so the
        # service stays up and flips readiness (graceful degradation, doc 06 §6.7).
        await self._pool.open()
        try:
            await self._pool.wait(timeout=self._policy.acquire_timeout_seconds)
            self.last_warmup_error = None
        except Exception as exc:  # noqa: BLE001
            # Startup stays graceful — the service comes up and reports degraded
            # rather than crash-looping while a database is briefly unreachable.
            #
            # But P4.3: the error is now RECORDED. Swallowing it made a
            # misconfigured DSN indistinguishable from a database that happened
            # to be down, which is the difference between "wait" and "fix your
            # config" — and the operator got neither signal.
            # OBSERVE (LOG): pool.warmup_failed {datasource, error}
            self.last_warmup_error = f"{type(exc).__name__}: {exc}"

    async def close(self) -> None:
        await self._pool.close()

    async def acquire(self) -> PsycopgConnection:
        try:
            conn = await self._pool.getconn()
        except psycopg.pool.PoolTimeout as exc:  # type: ignore[attr-defined]
            raise PoolTimeoutError(str(exc)) from exc
        return PsycopgConnection(conn)

    async def release(self, conn: ConnectionPort) -> None:
        # Widened to the port type. Narrowing to `PsycopgConnection` made this
        # an unsound override — the same defect already fixed in `MeteredPool`
        # (P4.2) — and it is what left `PsycopgPool` failing to satisfy
        # `PoolPort`, which in turn made `PsycopgDriver` fail `DriverPort`.
        # A pool can only return its own connections, so the check is an
        # assertion about internal consistency, not caller input.
        if not isinstance(conn, PsycopgConnection):
            raise TypeError(
                f"PsycopgPool cannot release a {type(conn).__name__}; "
                "connections must be returned to the pool that issued them"
            )
        await self._pool.putconn(conn.raw)

    async def health(self) -> HealthStatus:
        try:
            conn = await self.acquire()
            try:
                await conn.execute(QuerySpec("SELECT 1"))
            finally:
                await self.release(conn)
            self._consecutive_health_failures = 0
            return HealthStatus.HEALTHY
        except Exception as exc:  # noqa: BLE001
            self._consecutive_health_failures += 1
            # psycopg_pool's own background reconnect worker can permanently
            # give up after its first connect failure and never recover on its
            # own, even once the database becomes reachable again — a known
            # upstream issue (psycopg/psycopg#370), not something tunable via
            # PoolPolicy. Most likely to bite a freshly-started pod racing a
            # transient DNS/CNI warm-up window: found in real use, 2026-08-26 —
            # two out of three pods got stuck reporting "down" indefinitely
            # after one early "Temporary failure in name resolution", even
            # though a fresh connection attempt moments later succeeded fine.
            # Rebuilding the pool object after a few consecutive failures (not
            # on the first — that's likely ordinary transient contention, and
            # rebuilding is destructive to whatever connections the old pool
            # still holds) gives it a fresh reconnect worker rather than
            # trusting the stuck one.
            if self._consecutive_health_failures >= 3:
                self.last_warmup_error = f"{type(exc).__name__}: {exc}"
                await self._pool.close()
                self._pool = self._build_pool()
                await self.open()
                self._consecutive_health_failures = 0
            return HealthStatus.DOWN


class PsycopgDriver:
    """``DriverPort`` — the factory that builds psycopg pools."""

    def build_pool(self, dsn: str, policy: PoolPolicy) -> PsycopgPool:
        return PsycopgPool(dsn, policy)


__all__ = ["PsycopgDriver", "PsycopgPool", "PsycopgConnection"]

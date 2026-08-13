"""L2 — Unit of Work: an atomic transaction boundary (doc 07 §7.5).

Pins one connection for the whole transaction; commits on clean exit, rolls back
on any exception. Nested ``savepoint()`` uses SQL savepoints.
"""
from __future__ import annotations

from typing import Any, Sequence

from ...core.models import IsolationLevel, QuerySpec, ResultSet
from ...core.ports import PoolPort


class UnitOfWork:
    def __init__(self, pool: PoolPort, isolation: IsolationLevel | None = None) -> None:
        self._pool = pool
        self._isolation = isolation
        self._conn: Any = None
        self._pinned: Any = None
        self._txn: Any = None

    async def __aenter__(self) -> "UnitOfWork":
        self._conn = await self._pool.acquire()
        # P4.7 — the pinned-connection invariant, asserted rather than assumed.
        # Isolation is set on the CONNECTION by the driver, so a transaction that
        # did not own its connection exclusively would leak its isolation level
        # into whatever else touched it.
        assert self._conn is not None, "UnitOfWork must own exactly one connection"
        self._pinned = self._conn
        self._txn = self._conn.begin(self._isolation)
        await self._txn.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await self._txn.__aexit__(exc_type, exc, tb)  # commit or rollback
        finally:
            # Still the same connection we pinned: nothing swapped it underneath
            # the transaction (P4.7).
            assert self._conn is self._pinned, (
                "the connection changed during a transaction — isolation level "
                "and transaction state would both be corrupt"
            )
            await self._pool.release(self._conn)

    async def execute(self, spec: QuerySpec) -> ResultSet:
        return await self._conn.execute(spec)

    async def execute_many(self, spec: QuerySpec, params_seq: Sequence[Any]) -> int:
        return await self._conn.execute_many(spec, params_seq)

    def savepoint(self):
        """Nested transaction boundary (SQL SAVEPOINT)."""
        return self._conn.begin(None)


__all__ = ["UnitOfWork"]

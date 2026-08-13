"""In-memory fake driver — lets the whole stack be unit-tested with no DB.

Because every boundary is a Core port, tests inject this instead of psycopg
(doc 13 §13.2). Programmable responses + failures.
"""
from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator, Callable, Sequence

from ...core.errors import PoolTimeoutError
from ...core.models import (
    HealthStatus,
    IsolationLevel,
    PoolPolicy,
    QuerySpec,
    Row,
    ResultSet,
)

# A responder decides what a given SQL text returns (or raises).
Responder = Callable[[QuerySpec], ResultSet]


def _default_responder(spec: QuerySpec) -> ResultSet:
    if spec.sql.strip().upper().startswith("SELECT 1"):
        return ResultSet(rows=[{"?column?": 1}], rowcount=1)
    return ResultSet(rows=[], rowcount=0)


class FakeConnection:
    def __init__(self, responder: Responder, pool: "FakePool") -> None:
        self._responder = responder
        self._pool = pool

    async def execute(self, spec: QuerySpec) -> ResultSet:
        self._pool.executed.append(spec)
        return self._responder(spec)

    async def execute_many(self, spec: QuerySpec, params_seq: Sequence) -> int:
        self._pool.executed.append(spec)
        return len(list(params_seq))

    async def stream(
        self, spec: QuerySpec, *, itersize: int = 1000
    ) -> AsyncIterator[Row]:
        self._pool.executed.append(spec)
        for row in self._responder(spec).rows:
            yield row

    async def copy_stream(self, table: str, rows) -> int:
        n = 0
        if hasattr(rows, "__aiter__"):
            async for _ in rows:
                n += 1
        else:
            for _ in rows:
                n += 1
        self._pool.copied.append((table, n))
        return n

    async def listen(self, channel: str) -> AsyncIterator:
        for event in self._pool.notify_events:
            yield event

    @contextlib.asynccontextmanager
    async def begin(self, isolation: IsolationLevel | None = None):
        depth = self._pool.txn_depth
        self._pool.txn_depth += 1
        if depth == 0:
            self._pool.begins += 1
        try:
            yield self
        except Exception:
            if depth == 0:
                self._pool.rollbacks += 1
            self._pool.txn_depth -= 1
            raise
        else:
            if depth == 0:
                self._pool.commits += 1
            self._pool.txn_depth -= 1


class FakePool:
    def __init__(self, policy: PoolPolicy, responder: Responder) -> None:
        self.policy = policy
        self.responder = responder
        self.executed: list[QuerySpec] = []
        self.opened = False
        self.closed = False
        self.saturated = False  # set True to simulate pool exhaustion
        self.acquired = 0
        self.released = 0
        self.begins = 0
        self.commits = 0
        self.rollbacks = 0
        self.txn_depth = 0
        self.copied: list[tuple[str, int]] = []
        self.notify_events: list = []

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def acquire(self) -> FakeConnection:
        if self.saturated:
            raise PoolTimeoutError("fake pool saturated")
        self.acquired += 1
        return FakeConnection(self.responder, self)

    async def release(self, conn: FakeConnection) -> None:
        self.released += 1

    async def health(self) -> HealthStatus:
        return HealthStatus.DOWN if self.saturated else HealthStatus.HEALTHY


class FakeDriver:
    """``DriverPort`` producing in-memory pools with a shared responder."""

    def __init__(self, responder: Responder | None = None) -> None:
        self.responder = responder or _default_responder
        self.pools: dict[str, FakePool] = {}

    def build_pool(self, dsn: str, policy: PoolPolicy) -> FakePool:
        pool = FakePool(policy, self.responder)
        self.pools[dsn] = pool
        return pool


__all__ = ["FakeDriver", "FakePool", "FakeConnection", "Responder"]

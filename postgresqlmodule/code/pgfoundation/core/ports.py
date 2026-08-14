"""Core ports (interfaces) — the seams that make every outer piece swappable.

Defined with ``typing.Protocol`` so adapters need not inherit anything; the
psycopg adapter and the test fakes both satisfy these structurally (doc 03).
"""
from __future__ import annotations

from typing import (
    Any,
    AsyncContextManager,
    AsyncIterator,
    Protocol,
    Sequence,
    runtime_checkable,
)

from .models import HealthStatus, IsolationLevel, PoolPolicy, QuerySpec, ResultSet, Row


@runtime_checkable
class ConnectionPort(Protocol):
    """A single, usable connection. Adapters wrap the concrete driver conn."""

    async def execute(
        self, spec: QuerySpec, *, row_format: str = "dict"
    ) -> ResultSet: ...

    async def execute_many(
        self, spec: QuerySpec, params_seq: Sequence[Any]
    ) -> int: ...

    def stream(self, spec: QuerySpec, *, itersize: int = 1000) -> AsyncIterator[Row]:
        """Server-side cursor stream (doc 16 §16.3). Returns an async iterator."""
        ...

    def begin(
        self, isolation: IsolationLevel | None = None
    ) -> AsyncContextManager[Any]:
        """Transaction / savepoint context (nested = savepoint, doc 07 §7.5)."""
        ...

    # --- P2.11: declared here so the contract matches what callers already use.
    # `copy_stream` and `listen` were called by DataSourceHandle from the start
    # and implemented by every adapter, but were never part of the Protocol — so
    # nothing verified that an adapter actually satisfied the contract the handle
    # relies on (PORT-FINDINGS #1/#2).

    async def copy_stream(self, table: str, rows: Any) -> int:
        """Streaming bulk ingest via COPY. ``table`` is pre-validated."""
        ...

    def listen(self, channel: str) -> AsyncIterator[Any]:
        """LISTEN/NOTIFY change stream. ``channel`` is pre-validated."""
        ...

    def pipeline(self) -> AsyncContextManager[Any]:
        """Batch statements into fewer round-trips, where the driver supports it.

        Adapters that cannot pipeline return a no-op context manager: the caller's
        statements still execute correctly on this connection, just without the
        round-trip saving. So callers never branch on driver capability.
        """
        ...


@runtime_checkable
class PoolPort(Protocol):
    """A bounded, reusable set of connections for one data source (doc 06)."""

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def acquire(self) -> ConnectionPort: ...
    async def release(self, conn: ConnectionPort) -> None: ...
    async def health(self) -> HealthStatus: ...


@runtime_checkable
class DriverPort(Protocol):
    """Isolates psycopg (and any other PostgreSQL-compatible driver)."""

    def build_pool(self, dsn: str, policy: PoolPolicy) -> PoolPort: ...


# --- Observability seams (doc 10 §10.1 / ADR-014). Default no-ops. ---


@runtime_checkable
class LoggerPort(Protocol):
    def log(self, level: str, event: str, **fields: Any) -> None: ...


@runtime_checkable
class MetricsPort(Protocol):
    def incr(self, name: str, value: int = 1, **labels: str) -> None: ...
    def observe(self, name: str, value: float, **labels: str) -> None: ...
    def gauge(self, name: str, value: float, **labels: str) -> None: ...


@runtime_checkable
class TracerPort(Protocol):
    def span(self, name: str, **attrs: Any): ...  # returns a context manager


@runtime_checkable
class ClockPort(Protocol):
    """Injectable time — keeps timeout/backoff logic deterministic in tests."""

    def now(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


__all__ = [
    "ConnectionPort",
    "PoolPort",
    "DriverPort",
    "LoggerPort",
    "MetricsPort",
    "TracerPort",
    "ClockPort",
]

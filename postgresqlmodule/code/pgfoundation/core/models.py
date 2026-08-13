"""Core value objects — pure data, stdlib only (doc 03 Core).

These are the *only* types that cross the public boundary; no psycopg or Django
types ever leak out (doc 11).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

# A logical, configured data-source name (e.g. "orders-primary").
DataSourceName = str

# A mapped result row. Default mapping is dict (doc 07 §7.3).
Row = dict[str, Any]

# Query parameters: named mapping (%(name)s) or positional sequence (%s).
Params = Mapping[str, Any] | Sequence[Any] | None


class IsolationLevel(str, Enum):
    READ_COMMITTED = "READ COMMITTED"
    REPEATABLE_READ = "REPEATABLE READ"
    SERIALIZABLE = "SERIALIZABLE"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass(frozen=True)
class PoolPolicy:
    """Driver-agnostic pool tunables (doc 06 §6.3). Config maps onto this."""

    min_size: int = 1
    max_size: int = 10
    acquire_timeout_seconds: float = 5.0
    max_idle_seconds: float = 300.0
    max_lifetime_seconds: float = 1800.0
    statement_timeout_ms: int | None = None
    read_only: bool = False


@dataclass(frozen=True)
class QuerySpec:
    """SQL text + separately-bound parameters (never string-concatenated)."""

    sql: str
    params: Params = None

    def __post_init__(self) -> None:
        if not isinstance(self.sql, str) or not self.sql.strip():
            raise ValueError("QuerySpec.sql must be a non-empty string")


class Query(QuerySpec):
    """A read. Retry-safe; routable to replicas (CQRS-lite, doc 07 §7.4)."""


class Command(QuerySpec):
    """A write. Retried only when marked idempotent (doc 07 §7.4)."""


class FilterOp(str, Enum):
    """Whitelisted comparison operators for governed view filters (doc 07 §7.2)."""

    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"
    IN = "in"
    NOT_IN = "not_in"
    LIKE = "like"
    ILIKE = "ilike"
    BETWEEN = "between"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


@dataclass(frozen=True)
class Filter:
    """A single ``column <op> value`` predicate. Value(s) are always bound."""

    column: str
    op: FilterOp = FilterOp.EQ
    value: Any = None


@dataclass(frozen=True)
class OrderBy:
    column: str
    descending: bool = False


@dataclass(frozen=True)
class ViewQuery:
    """Governed, name-based read of a view/table — no raw SQL from the caller.

    Compiles to a safe, parameterized ``SELECT`` (doc 07 §7.2). ``columns=None``
    selects all columns.
    """

    name: str
    schema: str | None = None
    columns: Sequence[str] | None = None
    filters: Sequence[Filter] = ()
    order_by: Sequence[OrderBy] = ()
    limit: int | None = None
    offset: int | None = None


@dataclass(frozen=True)
class FunctionQuery:
    """Invoke a function by name. ``args`` may be a mapping (named) or a sequence

    (positional). ``scalar=True`` → ``SELECT f(...)``; otherwise ``SELECT * FROM f(...)``.
    """

    name: str
    args: Mapping[str, Any] | Sequence[Any] | None = None
    schema: str | None = None
    scalar: bool = False


@dataclass(frozen=True)
class ProcedureCall:
    """Invoke a stored procedure by name via ``CALL`` (positional ``args``).

    Procedures with ``INOUT``/``OUT`` params return a row with the output values.
    """

    name: str
    args: Sequence[Any] | None = None
    schema: str | None = None


@dataclass(frozen=True)
class Agg:
    """A measure + aggregation for an :class:`IntervalQuery` (doc 16 §16.4)."""

    column: str
    func: str = "sum"  # sum | avg | min | max | count
    alias: str | None = None


@dataclass(frozen=True)
class IntervalQuery:
    """Declarative time-bucketed / windowed fetch (doc 16 §16.4)."""

    source: str  # governed table/view (whitelisted)
    time_column: str
    start: Any
    end: Any
    every: str  # bucket width, e.g. "15 minutes"
    metrics: Sequence[Agg] = ()
    group_by: Sequence[str] = ()
    tz: str | None = None


@dataclass(frozen=True)
class WatermarkCursor:
    """Resumable keyset checkpoint for incremental fetch (doc 16 §16.5)."""

    order_by: tuple[str, ...] = ("ts", "id")
    after: tuple[Any, ...] | None = None


@dataclass
class Page:
    """A keyset page + the watermark to resume from."""

    rows: list[Row] = field(default_factory=list)
    next_watermark: WatermarkCursor | None = None


@dataclass(frozen=True)
class NotifyEvent:
    """A ``LISTEN/NOTIFY`` change event (doc 16 §16.6)."""

    channel: str
    payload: str
    pid: int


@dataclass(frozen=True)
class RetryPolicy:
    """Transaction-level retry for serialization failures / deadlocks (doc 07 §7.10)."""

    max_attempts: int = 3
    base_backoff_ms: float = 20.0
    max_backoff_ms: float = 1000.0
    jitter: bool = True


@dataclass
class ResultSet:
    """The outcome of executing a QuerySpec."""

    rows: list[Row] = field(default_factory=list)
    rowcount: int = 0
    elapsed_ms: float = 0.0

    def __iter__(self):
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)


__all__ = [
    "DataSourceName",
    "Row",
    "Params",
    "IsolationLevel",
    "HealthStatus",
    "PoolPolicy",
    "RetryPolicy",
    "QuerySpec",
    "Query",
    "Command",
    "ResultSet",
    "FilterOp",
    "Filter",
    "OrderBy",
    "ViewQuery",
    "FunctionQuery",
    "ProcedureCall",
    "Agg",
    "IntervalQuery",
    "WatermarkCursor",
    "Page",
    "NotifyEvent",
]

"""pgfoundation — PostgreSQL Database Foundation Server (core library).

The **only** import path consumers use. Everything under ``pgfoundation._internal``
is private and carries no stability promise (doc 11 §11.1).

    import asyncio
    from pgfoundation import DataFoundation, Query

    async def main():
        async with await DataFoundation.from_config("pgfoundation.yaml") as db:
            rs = await db.query("orders", Query("SELECT 1"))
            print(rs.rows)

    asyncio.run(main())
"""
from __future__ import annotations

from ._internal.access.repository import Repository
from ._internal.api.facade import DataFoundation, DataSourceHandle
from .core.errors import (
    ConfigError,
    ConnectionError,
    IntegrityError,
    PermanentError,
    PgFoundationError,
    PoolTimeoutError,
    QueryError,
    TransactionError,
    TransientError,
)
from .core.models import (
    Agg,
    Command,
    Filter,
    FilterOp,
    FunctionQuery,
    HealthStatus,
    IntervalQuery,
    IsolationLevel,
    NotifyEvent,
    OrderBy,
    Page,
    PoolPolicy,
    ProcedureCall,
    Query,
    QuerySpec,
    ResultSet,
    RetryPolicy,
    Row,
    ViewQuery,
    WatermarkCursor,
)

__version__ = "0.0.1"

__all__ = [
    # entry point + handle
    "DataFoundation",
    "DataSourceHandle",
    # value objects
    "Query",
    "Command",
    "QuerySpec",
    "ResultSet",
    "Row",
    # governed object invocation (call views/functions/procedures by name)
    "ViewQuery",
    "FunctionQuery",
    "ProcedureCall",
    "Filter",
    "FilterOp",
    "OrderBy",
    "IsolationLevel",
    "PoolPolicy",
    "RetryPolicy",
    "Repository",
    "HealthStatus",
    # streaming & interval (N4)
    "IntervalQuery",
    "Agg",
    "WatermarkCursor",
    "Page",
    "NotifyEvent",
    # error hierarchy (driver-agnostic)
    "PgFoundationError",
    "ConfigError",
    "ConnectionError",
    "PoolTimeoutError",
    "QueryError",
    "IntegrityError",
    "TransactionError",
    "TransientError",
    "PermanentError",
    "__version__",
]

"""v1 — DTO to core value object (ADR-003).

One mapper per operation. Kept out of ``operations.py`` so that the table there
stays **pure data** — which is what makes "adding a capability is adding a row"
literally true.

These functions are the only place v1 wire semantics meet core semantics, so a v2
that changes semantics overrides mappers rather than forking the engine
(doc 09 §9.1b).
"""

from __future__ import annotations

from typing import Any

from pgfoundation import (
    Agg,
    Command,
    Filter,
    FunctionQuery,
    IntervalQuery,
    OrderBy,
    ProcedureCall,
    Query,
    ViewQuery,
    WatermarkCursor,
)

from .schemas.bulk import CopyIn, ExecuteManyIn, QueryIn
from .schemas.interval import FetchSinceIn, IntervalIn
from .schemas.invocation import FunctionIn, ProcedureIn, ViewIn


def to_view_query(dto: ViewIn) -> ViewQuery:
    return ViewQuery(
        name=dto.name,
        schema=dto.db_schema,
        columns=dto.columns,
        filters=tuple(Filter(f.column, f.op, f.value) for f in dto.filters),
        order_by=tuple(OrderBy(o.column, o.descending) for o in dto.order_by),
        limit=dto.limit,
        offset=dto.offset,
    )


def to_function_query(dto: FunctionIn) -> FunctionQuery:
    return FunctionQuery(
        name=dto.name,
        args=dto.args,
        schema=dto.db_schema,
        scalar=dto.scalar,
    )


def to_procedure_call(dto: ProcedureIn) -> ProcedureCall:
    return ProcedureCall(name=dto.name, args=dto.args, schema=dto.db_schema)


def to_query(dto: QueryIn) -> Query:
    return Query(dto.sql, dto.params)


def to_command(dto: QueryIn) -> Command:
    return Command(dto.sql, dto.params)


def to_interval_query(dto: IntervalIn) -> IntervalQuery:
    return IntervalQuery(
        source=dto.source,
        time_column=dto.time_column,
        start=dto.start,
        end=dto.end,
        every=dto.every,
        metrics=tuple(Agg(m.column, m.func, m.alias) for m in dto.metrics),
        group_by=tuple(dto.group_by),
        tz=dto.tz,
    )


# --- adapters for operations whose handle call is not `method(one_argument)`.
# Supplied as OperationSpec.invoke so the table stays declarative (ADR-003).


async def invoke_fetch_since(handle: Any, dto: FetchSinceIn) -> Any:
    cursor = WatermarkCursor(
        order_by=tuple(dto.cursor.order_by),
        after=tuple(dto.cursor.after) if dto.cursor.after is not None else None,
    )
    return await handle.fetch_since(Query(dto.sql, dto.params), cursor, limit=dto.limit)


async def invoke_execute_many(handle: Any, dto: ExecuteManyIn) -> Any:
    return await handle.execute_many(Command(dto.sql, None), dto.params_seq)


async def invoke_copy(handle: Any, dto: CopyIn) -> Any:
    return await handle.copy_stream(dto.table, dto.rows)


__all__ = [
    "invoke_copy",
    "invoke_execute_many",
    "invoke_fetch_since",
    "to_command",
    "to_function_query",
    "to_procedure_call",
    "to_interval_query",
    "to_query",
    "to_view_query",
]

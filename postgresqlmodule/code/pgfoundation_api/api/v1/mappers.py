"""v1 — DTO to core value object (ADR-003).

One mapper per operation. Kept out of ``operations.py`` so that the table there
stays **pure data** — which is what makes "adding a capability is adding a row"
literally true.

These functions are the only place v1 wire semantics meet core semantics, so a v2
that changes semantics overrides mappers rather than forking the engine
(doc 09 §9.1b).
"""

from __future__ import annotations

from pgfoundation import (
    Command,
    Filter,
    FunctionQuery,
    OrderBy,
    ProcedureCall,
    Query,
    ViewQuery,
)

from .schemas.bulk import QueryIn
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


__all__ = [
    "to_command",
    "to_function_query",
    "to_procedure_call",
    "to_query",
    "to_view_query",
]

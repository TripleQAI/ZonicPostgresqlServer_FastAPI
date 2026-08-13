"""Governed object invocation — compile a *named* DB object call into safe SQL.

Callers name a **view**, **function**, or **procedure** and pass parameters; this
module (Strategy per object kind + a small SQL Builder) emits a **parameterized**
``QuerySpec``. No raw SQL crosses the boundary and values are always bound
(doc 07 §7.2), so there is no injection surface and no arbitrary-table access.

The generated SQL is **deterministic for a given call shape** (stable text, stable
param names) so psycopg's prepared-statement cache is reused across calls →
high throughput.
"""
from __future__ import annotations

from typing import Mapping

from ...core.errors import QueryError
from ...core.models import (
    Command,
    Filter,
    FilterOp,
    FunctionQuery,
    ProcedureCall,
    Query,
    ViewQuery,
)
from .sql import safe_identifier

# FilterOp -> SQL binary operator (only whitelisted ops reach here).
_BINARY_OP = {
    FilterOp.EQ: "=",
    FilterOp.NE: "<>",
    FilterOp.LT: "<",
    FilterOp.LE: "<=",
    FilterOp.GT: ">",
    FilterOp.GE: ">=",
    FilterOp.LIKE: "LIKE",
    FilterOp.ILIKE: "ILIKE",
}


def _qualified(name: str, schema: str | None) -> str:
    """Validate + qualify an object name (``schema.name`` or ``name``)."""
    return safe_identifier(f"{schema}.{name}" if schema else name)


def _compile_filters(filters) -> tuple[str, dict]:
    if not filters:
        return "", {}
    clauses: list[str] = []
    params: dict = {}
    for i, f in enumerate(filters):
        if not isinstance(f, Filter):
            raise QueryError("filters must be Filter instances")
        col = safe_identifier(f.column)
        op = f.op
        if op in (FilterOp.IS_NULL, FilterOp.IS_NOT_NULL):
            clauses.append(f"{col} IS {'NOT ' if op is FilterOp.IS_NOT_NULL else ''}NULL")
        elif op in (FilterOp.IN, FilterOp.NOT_IN):
            values = list(f.value or [])
            if not values:
                raise QueryError(f"{op.value} filter requires a non-empty list")
            placeholders = ", ".join(f"%(f{i}_{j})s" for j in range(len(values)))
            for j, v in enumerate(values):
                params[f"f{i}_{j}"] = v
            kw = "NOT IN" if op is FilterOp.NOT_IN else "IN"
            clauses.append(f"{col} {kw} ({placeholders})")
        elif op is FilterOp.BETWEEN:
            try:
                lo, hi = f.value
            except (TypeError, ValueError):
                raise QueryError("between filter requires a [low, high] pair") from None
            params[f"f{i}_lo"], params[f"f{i}_hi"] = lo, hi
            clauses.append(f"{col} BETWEEN %(f{i}_lo)s AND %(f{i}_hi)s")
        else:
            sql_op = _BINARY_OP.get(op)
            if sql_op is None:
                raise QueryError(f"unsupported filter op: {op}")
            params[f"f{i}"] = f.value
            clauses.append(f"{col} {sql_op} %(f{i})s")
    return " WHERE " + " AND ".join(clauses), params


def compile_view(q: ViewQuery) -> Query:
    """ViewQuery -> parameterized ``SELECT`` over a whitelisted relation."""
    src = _qualified(q.name, q.schema)
    cols = "*" if not q.columns else ", ".join(safe_identifier(c) for c in q.columns)

    where, params = _compile_filters(q.filters)

    order = ""
    if q.order_by:
        order = " ORDER BY " + ", ".join(
            f"{safe_identifier(o.column)} {'DESC' if o.descending else 'ASC'}"
            for o in q.order_by
        )

    tail = ""
    if q.limit is not None:
        tail += f" LIMIT {int(q.limit)}"
    if q.offset is not None:
        tail += f" OFFSET {int(q.offset)}"

    return Query(f"SELECT {cols} FROM {src}{where}{order}{tail}", params)


def compile_function(q: FunctionQuery) -> Query:
    """FunctionQuery -> ``SELECT * FROM f(...)`` (rows) or ``SELECT f(...)`` (scalar)."""
    fn = _qualified(q.name, q.schema)
    params: dict = {}

    if q.args is None:
        arglist = ""
    elif isinstance(q.args, Mapping):
        parts = []
        for key, value in q.args.items():
            arg = safe_identifier(key)  # named-arg identifier
            parts.append(f"{arg} => %({arg})s")
            params[arg] = value
        arglist = ", ".join(parts)
    else:
        parts = []
        for i, value in enumerate(q.args):
            parts.append(f"%(a{i})s")
            params[f"a{i}"] = value
        arglist = ", ".join(parts)

    if q.scalar:
        return Query(f"SELECT {fn}({arglist}) AS result", params)
    return Query(f"SELECT * FROM {fn}({arglist})", params)


def compile_procedure(q: ProcedureCall) -> Command:
    """ProcedureCall -> ``CALL p(...)`` (positional args; NULLs for OUT params)."""
    proc = _qualified(q.name, q.schema)
    params: dict = {}
    if not q.args:
        arglist = ""
    else:
        parts = []
        for i, value in enumerate(q.args):
            parts.append(f"%(a{i})s")
            params[f"a{i}"] = value
        arglist = ", ".join(parts)
    return Command(f"CALL {proc}({arglist})", params)


__all__ = ["compile_view", "compile_function", "compile_procedure"]

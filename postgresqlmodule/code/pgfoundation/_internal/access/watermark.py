"""Incremental / watermark fetching — keyset, never OFFSET (doc 16 §16.5).

Wraps a base query as a subquery, applies a tuple keyset predicate, orders by the
watermark key, and returns a page + the next resume point.
"""
from __future__ import annotations

from ...core.errors import QueryError
from ...core.models import Page, Query, WatermarkCursor
from .sql import safe_identifier


def build_keyset_query(spec: Query, cursor: WatermarkCursor, limit: int) -> Query:
    if not isinstance(spec.params, dict) and spec.params is not None:
        raise QueryError("fetch_since requires named (dict) params on the base query")
    if limit <= 0:
        raise QueryError("limit must be positive")

    order_cols = [safe_identifier(c) for c in cursor.order_by]
    order_clause = ", ".join(order_cols)
    params = dict(spec.params or {})

    where = ""
    if cursor.after is not None:
        if len(cursor.after) != len(order_cols):
            raise QueryError("watermark.after length must match order_by")
        lhs = "(" + ", ".join(order_cols) + ")"
        rhs = "(" + ", ".join(f"%(__k{i})s" for i in range(len(cursor.after))) + ")"
        where = f"WHERE {lhs} > {rhs} "
        for i, value in enumerate(cursor.after):
            params[f"__k{i}"] = value

    sql = (
        f"SELECT * FROM ({spec.sql}) AS __sub "
        f"{where}"
        f"ORDER BY {order_clause} "
        f"LIMIT {int(limit)}"
    )
    return Query(sql, params)


def next_watermark(rows: list[dict], cursor: WatermarkCursor) -> WatermarkCursor:
    if not rows:
        return cursor
    last = rows[-1]
    # order_by may be "schema.col"; the mapped row key is the bare column name.
    after = tuple(last[c.split(".")[-1]] for c in cursor.order_by)
    return WatermarkCursor(order_by=cursor.order_by, after=after)


async def fetch_since(handle, spec: Query, cursor: WatermarkCursor, *, limit: int) -> Page:
    keyset = build_keyset_query(spec, cursor, limit)
    rs = await handle.query(keyset)
    return Page(rows=rs.rows, next_watermark=next_watermark(rs.rows, cursor))


__all__ = ["fetch_since", "build_keyset_query", "next_watermark"]

"""Interval / windowed fetching — the generic bucketing Strategy (doc 16 §16.4).

Compiles a declarative :class:`IntervalQuery` into a parameterized ``QuerySpec``
using generic PostgreSQL (``date_bin`` + aggregates). A future time-series
capability can supply a TimescaleDB ``time_bucket`` Strategy behind this seam.
"""
from __future__ import annotations

from ...core.models import IntervalQuery, Query
from .sql import quote_identifier, safe_agg_func, safe_identifier


def compile_interval(q: IntervalQuery) -> Query:
    """IntervalQuery -> parameterized Query (half-open ``[start, end)``)."""
    src = quote_identifier(q.source)
    tcol = quote_identifier(q.time_column)

    # date_bin buckets any interval width against a stable origin (PG14+).
    bucket = f"date_bin(%(__every)s::interval, {tcol}, %(__start)s) AS bucket"

    select_parts = [bucket]
    group_parts = ["bucket"]
    for g in q.group_by:
        gi = quote_identifier(g)
        select_parts.append(gi)
        group_parts.append(gi)
    for m in q.metrics:
        col = quote_identifier(m.column)
        func = safe_agg_func(m.func)
        # An alias is a single name, never qualified: `AS "t"."value"` is a
        # syntax error, so a defaulted alias takes the last segment of the
        # column (`t.value` -> `"value"`).
        alias = quote_identifier(
            m.alias if m.alias else safe_identifier(m.column).split(".")[-1]
        )
        select_parts.append(f"{func}({col}) AS {alias}")

    sql = (
        f"SELECT {', '.join(select_parts)} "
        f"FROM {src} "
        f"WHERE {tcol} >= %(__start)s AND {tcol} < %(__end)s "
        f"GROUP BY {', '.join(group_parts)} "
        f"ORDER BY bucket"
    )
    params = {"__every": q.every, "__start": q.start, "__end": q.end}
    return Query(sql, params)


__all__ = ["compile_interval"]
